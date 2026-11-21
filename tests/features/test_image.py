import base64
import io
import json
import os

import numpy as np
import pytest

pytest.importorskip("PIL")
pytest.importorskip("imagehash")

from PIL import Image  # noqa: E402

from ftf.contract.models import ArtefactType, Observed  # noqa: E402
from ftf.features.common import dataset_from_files, load_vectors  # noqa: E402
from ftf.features.image import apply, features_for_bytes, features_for_image  # noqa: E402
from ftf.features.image import hashing, har  # noqa: E402
from ftf.features.image.hashing import SkipImage, hamming, hashes, hue_rotate, load  # noqa: E402


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setenv("FTF_IMAGE_EMBED", "0")
    monkeypatch.setattr("ftf.features.image.embed._EMBEDDER", None)
    monkeypatch.setattr("ftf.features.image.embed._FAILED", False)


def creative(w=320, h=240, seed=0) -> Image.Image:
    """Colourful synthetic picture with structure at several scales."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    a = np.stack(
        [
            127 + 120 * np.sin(x / 23 + seed),
            127 + 120 * np.cos(y / 17 - seed),
            127 + 120 * np.sin((x + y) / 31),
        ],
        axis=-1,
    )
    for _ in range(6):
        x0, y0 = rng.integers(0, w - 60), rng.integers(0, h - 60)
        a[y0 : y0 + 50, x0 : x0 + 50] = rng.integers(0, 255, 3)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def png_bytes(img: Image.Image, fmt="PNG", **kw) -> bytes:
    b = io.BytesIO()
    img.save(b, format=fmt, **kw)
    return b.getvalue()


def test_hashes_shape_and_hue_invariance():
    img = creative()
    h = hashes(img)
    assert set(h) == {"image_phash", "image_dhash", "image_pdq"}
    assert len(h["image_phash"]) == 16 and len(h["image_dhash"]) == 16 and len(h["image_pdq"]) == 64
    for deg in (2, 5, 8, 15):  # the kits randomise 2-8 deg
        r = hashes(hue_rotate(img, deg))
        # CSS luma is preserved by hue-rotate up to clipping, so the luma hashes barely move
        # (the synthetic picture is fully saturated, so clipping moves a few bits)
        assert hamming(h["image_phash"], r["image_phash"]) <= 4, deg
        assert hamming(h["image_dhash"], r["image_dhash"]) <= 4, deg


def test_hue_rotation_preserves_css_luma():
    # moderately saturated, so 8-bit clipping (the only source of change) stays rare
    img = Image.blend(creative(), Image.new("RGB", (320, 240), (128, 128, 128)), 0.5)
    a = np.asarray(hashing.luma(img), dtype=int)
    b = np.asarray(hashing.luma(hue_rotate(img, 120)), dtype=int)
    assert np.abs(a - b).mean() < 1


def test_different_images_differ():
    a, b = hashes(creative(seed=1)), hashes(creative(seed=7))
    assert hamming(a["image_pdq"], b["image_pdq"]) > 40


def test_pdq_resize_stable_and_matches_reference_if_available():
    img = creative(640, 480)
    h1, q = hashing.pdq(img)
    h2, _ = hashing.pdq(img.resize((320, 240), Image.Resampling.LANCZOS))
    assert 0 <= q <= 100
    assert hamming(h1, h2) <= 20
    pdqhash = pytest.importorskip("pdqhash")
    ref, _ = pdqhash.compute(np.asarray(img))
    # pdqhash returns the bits most significant first, i.e. in the order of the hex string
    ref_hex = f"{int(''.join(str(int(b)) for b in ref), 2):064x}"
    assert h1 == ref_hex


def test_box_filter_matches_reference_loop():
    a = np.arange(23, dtype=np.float64) ** 1.5
    for window in (1, 2, 5, 8):
        half = (window + 2) // 2
        out, s, cnt, li, ri, oi = np.zeros_like(a), 0.0, 0, 0, 0, 0
        for _ in range(half - 1):
            s += a[ri]; cnt += 1; ri += 1  # noqa: E702
        for _ in range(window - half + 1):
            s += a[ri]; cnt += 1; out[oi] = s / cnt; ri += 1; oi += 1  # noqa: E702
        for _ in range(len(a) - window):
            s += a[ri] - a[li]; out[oi] = s / cnt; li += 1; ri += 1; oi += 1  # noqa: E702
        for _ in range(half - 1):
            s -= a[li]; cnt -= 1; out[oi] = s / cnt; li += 1; oi += 1  # noqa: E702
        np.testing.assert_allclose(hashing._box1d(a, window), out)


def test_skip_rules(tmp_path, monkeypatch):
    with pytest.raises(SkipImage, match="too_small"):
        load(png_bytes(Image.new("RGB", (39, 400), "red")))
    with pytest.raises(SkipImage, match="flat"):
        hashes(Image.new("RGB", (200, 200), (10, 200, 30)))
    with pytest.raises(SkipImage, match="not_raster"):
        load(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    with pytest.raises(SkipImage, match="not_raster"):
        load(b"%PDF-1.4 not an image")
    monkeypatch.setattr(hashing, "MAX_PIXELS", 1000)
    with pytest.raises(SkipImage, match="too_large"):
        load(png_bytes(creative()))
    assert features_for_bytes(b"\x89PNG broken") == []


def test_alpha_and_animated_first_frame(tmp_path):
    rgba = creative().convert("RGBA")
    rgba.putalpha(128)
    assert load(png_bytes(rgba)).rgb.mode == "RGB"
    frames = [creative(seed=1), creative(seed=9)]
    p = tmp_path / "anim.gif"
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=100, loop=0)
    first = {f.kind: f.value for f in features_for_image(p)}
    alone = hashes(load(png_bytes(frames[0], "GIF")).rgb)
    assert first["image_phash"] == alone["image_phash"]


def test_reencode_and_resize_change_little():
    img = creative(480, 360)
    h = hashes(img)
    for data in (
        png_bytes(img, "JPEG", quality=60),
        png_bytes(img, "WEBP", quality=60),
        png_bytes(img.resize((240, 180), Image.Resampling.BILINEAR)),
    ):
        r = hashes(load(data).rgb)
        assert hamming(h["image_phash"], r["image_phash"]) <= 6


def har_doc(entries):
    return json.dumps({"log": {"version": "1.2", "entries": entries}})


def har_entry(url, mime, body: bytes, encoding="base64"):
    text = base64.b64encode(body).decode() if encoding == "base64" else body.decode("latin-1")
    content = {"mimeType": mime, "size": len(body), "text": text}
    if encoding:
        content["encoding"] = encoding
    return {"request": {"url": url}, "response": {"content": content}}


def test_har_images_only_base64_rasters(tmp_path):
    img = png_bytes(creative())
    p = tmp_path / "x.har"
    p.write_text(
        har_doc(
            [
                har_entry("https://a.example/1.png", "image/png", img),
                har_entry("https://a.example/d.svg", "image/svg+xml", b"<svg/>"),
                har_entry("https://a.example/t.js", "application/javascript", b"x()"),
                har_entry("https://a.example/raw.png", "image/png", b"abc", encoding=None),
                {"request": {}, "response": None},
            ]
        )
    )
    got = list(har.iter_images(p))
    assert [(i, u) for i, u, _ in got] == [(0, "https://a.example/1.png")]
    assert got[0][2] == img
    (tmp_path / "bad.har").write_text("{not json")
    assert list(har.iter_images(tmp_path / "bad.har")) == []


def test_apply_files_and_har(tmp_path):
    raw = tmp_path / "raw"
    case = raw / "case" / "page_files"
    case.mkdir(parents=True)
    img = creative()
    (case / "hero.webp.pobrane").write_bytes(png_bytes(img, "WEBP", quality=90))
    (case / "icon.png").write_bytes(png_bytes(Image.new("RGB", (16, 16), "blue")))
    (raw / "case" / "shot.png").write_bytes(png_bytes(creative(seed=3)))
    (raw / "case" / "page.har").write_text(
        har_doc([har_entry("https://x.example/hero.png", "image/png", png_bytes(img))])
    )
    ds = dataset_from_files(sorted(p for p in raw.rglob("*") if p.is_file()), raw)
    # in the dev wrapper the odd-suffix asset is typed "other"; make it an asset as ingestion does
    for inc in ds.incidents:
        for a in inc.captures[0].artefacts:
            if a.path.endswith(".pobrane"):
                a.type = ArtefactType.asset
    out = tmp_path / "out"
    ds = apply(ds, raw, out)
    by_path = {inc.source_ref: inc.indicators for inc in ds.incidents}
    assert sorted(i.kind for i in by_path["case/page_files/hero.webp.pobrane"]) == [
        "image_dhash",
        "image_pdq",
        "image_phash",
    ]
    assert by_path["case/page_files/icon.png"] == []  # below the minimum size
    assert len(by_path["case/shot.png"]) == 3
    har_inds = by_path["case/page.har"]
    assert {i.observed for i in har_inds} == {Observed.live}
    assert har_inds[0].provenance.locator == "har_entry=0"
    assert har_inds[0].provenance.note == "https://x.example/hero.png"
    # same picture via saved file (WebP q90) and HAR (PNG): pHash stays close
    ph = {k: next(i.value for i in v if i.kind == "image_phash") for k, v in by_path.items() if v}
    assert hamming(ph["case/page_files/hero.webp.pobrane"], ph["case/page.har"]) <= 4
    # re-running replaces, not duplicates
    ds = apply(ds, raw, out)
    assert len([i for inc in ds.incidents for i in inc.indicators]) == 9


def test_duplicate_bytes_emitted_once_per_capture(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    data = png_bytes(creative())
    (raw / "a.png").write_bytes(data)
    (raw / "b.png").write_bytes(data)
    ds = dataset_from_files([raw / "a.png", raw / "b.png"], raw)
    cap0 = ds.incidents[0].captures[0]
    cap0.artefacts.append(ds.incidents[1].captures[0].artefacts[0].model_copy(update={"id": "dev/x/a1"}))
    ds.incidents = ds.incidents[:1]
    ds = apply(ds, raw, tmp_path / "out")
    assert len(ds.incidents[0].indicators) == 3


@pytest.mark.skipif(os.environ.get("FTF_TEST_MODELS") != "1", reason="downloads model weights")
def test_embedding_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    monkeypatch.setenv("FTF_IMAGE_EMBED", "1")
    p = tmp_path / "c.png"
    creative().save(p)
    feats = features_for_image(p)
    emb = [f for f in feats if f.kind == "image_embedding"]
    assert len(emb) == 1 and emb[0].vector.shape == (768,)
    assert abs(float(np.linalg.norm(emb[0].vector)) - 1) < 1e-4
    tall = tmp_path / "tall.png"
    creative(200, 900).save(tall)
    assert next(f for f in features_for_image(tall) if f.kind == "image_embedding").locator == "crop=top:400px"
    ds = dataset_from_files([p], tmp_path)
    ds = apply(ds, tmp_path, tmp_path / "out")
    ref = next(i.vector_ref for i in ds.incidents[0].indicators if i.vector_ref)
    keys, rows = load_vectors(tmp_path / "out", ref.store)
    assert keys == [ref.key] and rows.shape == (1, 768)
