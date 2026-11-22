import json
import os
from pathlib import Path

import numpy as np
import pytest

from ftf.contract.models import Artefact, ArtefactType, Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Provenance, Stage
from ftf.features import faces
from ftf.features.common import load_vectors, write_dataset, read_dataset
from ftf.features.faces import brands, models, reference
from ftf.features.faces.brands import html_title_text, match_brands
from ftf.features.faces.reference import Reference

MANIFEST = Path(faces.__file__).parent / "reference_manifest.json"


def names(text):
    return sorted(b.name for b, _ in match_brands(text))


def test_brand_gazetteer():
    assert names("Czytaj więcej na www.onet.pl") == ["Onet"]
    assert names("TVN24 | Fakty") == ["TVN24"]
    assert names("Wywiad dnia – Polsat News") == ["Polsat News"]
    assert names("«Przykładowy nagłówek» — Gazeta.pl") == ["Gazeta.pl"]
    assert names("Bank Pekao S.A. i PKO BP") == ["Bank Pekao", "PKO Bank Polski"]
    assert names("Odbierz paczkę z Paczkomatu InPost") == ["InPost"]
    # common words and lowercase short names do not count
    assert names("Fakt jest taki, że wp ing zus pko") == []
    assert names("FAKT") == ["Fakt"]
    assert names("artefakt.pl tvnet") == []


def test_brands_in_merges_sources():
    found = brands.brands_in([("ocr_text", "TVN24 news"), ("html_title", "tvn24.pl")])
    brand, sources = found["TVN24"]
    assert brand.category == "tv" and [s.split(":")[0] for s in sources] == ["ocr_text", "html_title"]


def test_html_title_is_read_inert(tmp_path):
    p = tmp_path / "page.html"
    p.write_bytes(
        b"<html><head><script>alert(1)</script><title>Przyk&#322;adowy tytu&#322; &mdash; Gazeta.pl</title>"
        b'<meta property="og:site_name" content="Gazeta.pl"></head><body>x</body></html>'
    )
    assert html_title_text(p) == "Przykładowy tytuł — Gazeta.pl | Gazeta.pl"
    (tmp_path / "none.html").write_bytes(b"<p>no title</p>")
    assert html_title_text(tmp_path / "none.html") is None


def test_reference_match_takes_best_image_per_person():
    m = np.array([[1, 0, 0], [0, 1, 0], [0.6, 0.8, 0]], dtype=np.float32)
    ref = Reference(["A", "B", "A"], ["f1", "f2", "f3"], m, "sha")
    top = ref.match(np.array([0.0, 2.0, 0.0]), k=3)
    assert [n for n, _ in top] == ["B", "A"]
    assert top[0][1] == pytest.approx(1.0) and top[1][1] == pytest.approx(0.8)
    assert ref.match(np.zeros(3)) == []


def test_git_manifest_lists_names_sources_and_files():
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert doc["format"] == reference.FORMAT and doc["images"]
    for im in doc["images"]:
        assert im["name"] and im["source"].startswith("https://") and im["file"].startswith("images/")
        assert len(im["sha256"]) == 64


def test_read_names_skips_comments_and_pins_qids(tmp_path):
    p = tmp_path / "names.txt"
    p.write_text("# people\nExample Person A\n\nExample Person B=Q42\n", encoding="utf-8")
    assert reference.read_names(p) == [("Example Person A", None), ("Example Person B", "Q42")]


def test_load_reference_without_manifest_notes_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FTF_FACE_REFERENCE", str(tmp_path / "missing"))
    reference.load_reference.cache_clear()
    assert reference.load_reference() is None
    assert reference.load_reference() is None
    err = capsys.readouterr().err
    assert err.count("no reference set") == 1


def test_load_reference_reads_prebuilt_directory(tmp_path, monkeypatch):
    doc = {"format": 1, "embeddings": "embeddings.npy",
           "images": [{"name": "Example Person A", "source": "https://x.example/a", "file": "images/a.jpg"},
                      {"name": "Example Person B", "source": "https://x.example/b", "file": "images/b.jpg"}]}
    (tmp_path / "manifest.json").write_text(json.dumps(doc))
    np.save(tmp_path / "embeddings.npy", np.eye(3, dtype=np.float32)[:2])
    monkeypatch.setenv("FTF_FACE_REFERENCE", str(tmp_path))
    reference.load_reference.cache_clear()
    ref = reference.load_reference()
    assert ref.people == ["Example Person A", "Example Person B"]
    assert ref.sources == ["https://x.example/a", "https://x.example/b"]
    assert ref.match(np.array([0.0, 0.0, 5.0]))[0][1] == pytest.approx(0.0)
    reference.load_reference.cache_clear()
    (tmp_path / "embeddings.npy").unlink()
    monkeypatch.setattr(reference, "_embed_rows", lambda d, rows: (rows[:1], np.eye(3, dtype=np.float32)[:1]))
    assert reference.load_reference().people == ["Example Person A"]
    reference.load_reference.cache_clear()


class StubEngine:
    def faces(self, bgr):
        return [models.Face(box=(1, 2, 30, 40), score=0.95, embedding=np.array([0.0, 3.0, 0.0], np.float32))]


@pytest.fixture
def stubbed(monkeypatch):
    ref = Reference(["Person A", "Person B"], ["f1", "f2"], np.eye(3, dtype=np.float32)[:2], "abc")
    monkeypatch.setattr(faces, "_engine_cache", StubEngine())
    monkeypatch.setattr(models, "load_bgr", lambda p: np.zeros((4, 4, 3), np.uint8))
    monkeypatch.setattr(reference, "load_reference", lambda: ref)
    monkeypatch.setattr(faces, "ocr_text", lambda p, timeout=120: "Tekst testowy Kanał Zero")


def test_apply_emits_faces_people_and_brands(tmp_path, stubbed):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ad.png").write_bytes(b"x" * 20_000)
    (raw / "page.html").write_bytes(b"<title>Fakty TVN24</title>")
    arts = [
        Artefact(id="c1/a0", type=ArtefactType.ad_screenshot, path="ad.png"),
        Artefact(id="c1/a1", type=ArtefactType.html, path="page.html"),
    ]
    cap = Capture(id="c1", stage=Stage.lander, artefacts=arts)
    inc = Incident(id="i1", source="t", captures=[cap])
    inc.indicators.append(
        Indicator(id="c1/page_text/0", capture_id="c1", layer=Layer.creative, kind="page_text",
                  level=Level.campaign, value="Tekst testowy Bank Pekao", observed=Observed.static,
                  provenance=Provenance(artefact_id="c1/a1"), extractor="text@0.1")
    )
    ds = Dataset(name="t", incidents=[inc])
    out = tmp_path / "out"
    ds = faces.apply(ds, raw, out)
    inds = {(i.kind, i.value or "") : i for i in ds.incidents[0].indicators if i.extractor == faces.EXTRACTOR}
    pf = inds[("person_face", "Person B")]
    assert pf.provenance.note.startswith("sface_cosine=1.0000;top3=Person B:1.0000|Person A:0.0000")
    assert pf.provenance.locator == "face=0;bbox=1,2,30,40"
    emb = inds[("face_embedding", "")]
    keys, mat = load_vectors(out, emb.vector_ref.store)
    assert keys == [emb.id] and mat.shape == (1, 3) and emb.vector_ref.model == models.EMBEDDING_MODEL
    brand_vals = {v for k, v in inds if k == "brand"}
    assert brand_vals == {"TVN24", "Bank Pekao", "Kanał Zero"}
    assert inds[("brand", "TVN24")].provenance.artefact_id == "c1/a1"
    assert inds[("brand", "Kanał Zero")].provenance.locator == "src=ocr"
    stats = json.loads((out / "faces-stats.json").read_text())
    assert stats["images"] == 1 and stats["faces"] == 1 and stats["ocr_fallback"] == 1
    read_dataset(write_dataset(ds, out))  # contract-valid

    # a re-run replaces the family's output instead of duplicating it
    n = len(ds.incidents[0].indicators)
    ds = faces.apply(ds, raw, out)
    assert len(ds.incidents[0].indicators) == n


def test_features_for_image(tmp_path, stubbed):
    p = tmp_path / "frame.png"
    p.write_bytes(b"x")
    feats = faces.features_for_image(p)
    assert [f.kind for f in feats] == ["face_embedding", "person_face", "brand"]
    assert faces.features_for_image(p, text="InPost")[-1].value == "InPost"


@pytest.mark.skipif(os.environ.get("FTF_OFFLINE") == "1", reason="needs the model download")
def test_real_engine_on_blank_and_tall_image():
    pytest.importorskip("cv2")
    eng = models.FaceEngine()
    assert eng.faces(np.full((480, 640, 3), 255, np.uint8)) == []
    assert eng.faces(np.full((6000, 1280, 3), 128, np.uint8)) == []


def test_missing_engine_skips_faces_and_keeps_brands(tmp_path, monkeypatch, capsys):
    def no_cv2():
        raise ImportError("No module named 'cv2'")

    monkeypatch.setattr(faces, "_engine", no_cv2)
    assert faces._engine_or_none() is None
    p = tmp_path / "frame.png"
    p.write_bytes(b"x")
    feats = faces.features_for_image(p, text="InPost")
    assert [f.kind for f in feats] == ["brand"]
    assert "no face engine" in capsys.readouterr().err


def test_offline_weights_are_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_OFFLINE", "1")
    monkeypatch.setattr(models, "model_dir", lambda: tmp_path)

    def no_network(*a, **k):
        raise AssertionError("download attempted while FTF_OFFLINE=1")

    monkeypatch.setattr(models.urllib.request, "urlopen", no_network)
    with pytest.raises(OSError, match="FTF_OFFLINE"):
        models.fetch_weights(models.YUNET)
