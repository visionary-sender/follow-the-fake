import base64
import json
import shutil

import numpy as np
import pytest

from ftf.contract.models import ArtefactType
from ftf.features import pipeline
from ftf.features.common import dataset_from_files, write_dataset
from ftf.features.text import keywords as kw
from ftf.features.text import template as tpl
from ftf.features.text.mask import mask, tokens

STORY = (
    "Niezwykła rozmowa: {first} {last} zdradza swój sposób na oszczędzanie! {first2} {last2} "
    "opowiedział w programie {outlet}, że zarobił 12 000 zł w 3 dni dzięki nowej platformie "
    "inwestycyjnej. Minimalny depozyt to 1 234 zł. Rejestracja trwa do 15.10.2026. Bank centralny "
    "próbował zablokować tę informację, ale miliony Polaków już zarabiają."
)


def story(first, last, first2, last2, outlet):
    return STORY.format(first=first, last=last, first2=first2, last2=last2, outlet=outlet)


A = story("Igi", "Świątek", "Iga", "Świątek", "Radio ZET")
B = story("Adama", "Małysza", "Adam", "Małysz", "Newsweek")
OTHER = (
    "Kardiolog ostrzega: ten prosty sposób obniża ciśnienie w tydzień. Kapsułki dostępne tylko "
    "dziś w promocji, lekarze są zaskoczeni wynikami badań nad nadciśnieniem i cukrzycą."
)


def test_mask_swaps_celebrity_to_same_text():
    a, b = mask(A), mask(B)
    assert a.text == b.text
    assert "<PERSON>" in a.text and "<PRICE>" in a.text and "<DATE>" in a.text and "<OUTLET>" in a.text
    assert a.people == ["Iga Świątek"] and b.people == ["Adam Małysz"]
    assert "Świąt" not in a.text and "1 234" not in a.text


def test_mask_reports_unknown_first_name_pairs():
    m = mask("Wywiad z Janem Kowalskim o inwestycjach. Przykładowa Grupa Handlowa sponsoruje.")
    assert "Janem Kowalskim" in m.other_names
    assert all("Grupa" not in n for n in m.other_names)
    assert "Kowalskim" not in m.text


def test_template_signature_tracks_masked_jaccard():
    s3, s4, so = (tpl.shingles(tokens(mask(t).text)) for t in (A, B, OTHER))
    raw = tpl.jaccard(tpl.shingles(tokens(A)), tpl.shingles(tokens(B)))
    assert tpl.jaccard(s3, s4) == 1.0 > raw
    h3, h4, ho = (tpl.to_hex(tpl.minhash(s)) for s in (s3, s4, so))
    assert len(h3) == tpl.NUM_PERM * 8 and np.array_equal(tpl.from_hex(h3), tpl.minhash(s3))
    assert tpl.estimate(h3, h4) == 1.0
    assert tpl.estimate(h3, ho) < 0.1


def test_keywords_and_topic():
    assert "inwestycja" in kw.keywords(A) and "depozyt" in kw.keywords(A)
    assert kw.topic(A) == "investment"
    assert kw.topic(OTHER) == "health"
    assert kw.topic("Ala ma kota.") is None


HTML = """<html><head><title>T</title><style>.x{}</style><script>var s="SECRET";</script></head>
<body><div hidden>HIDDEN1</div><p style="display: none">HIDDEN2</p><span aria-hidden="true">HIDDEN3</span>
<noscript>NOSCRIPT</noscript><template><p>TPL</p></template><input type="hidden" value="H">
<h1>Wielki <b>skandal</b></h1><p>Pierwszy akapit.</p><!-- COMMENT --><ul><li>raz</li><li>dwa</li></ul>
</body></html>"""


def test_visible_text_drops_hidden_and_code():
    pytest.importorskip("bs4")
    pytest.importorskip("lxml")
    from ftf.features.text.page import visible_text

    t = visible_text(HTML.encode())
    assert t.splitlines() == ["Wielki skandal", "Pierwszy akapit.", "raz", "dwa"]
    for bad in ("SECRET", "HIDDEN", "NOSCRIPT", "TPL", "COMMENT", ".x{}"):
        assert bad not in t


def test_mhtml_and_har_main_document(tmp_path):
    pytest.importorskip("bs4")
    pytest.importorskip("lxml")
    from ftf.features.text.page import page_text

    qp = HTML.replace("=", "=3D")
    mhtml = (
        "From: <Saved by Blink>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/related; "
        'boundary="B"\r\n\r\n--B\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: '
        f"quoted-printable\r\n\r\n{qp}\r\n--B--\r\n"
    )
    (tmp_path / "p.mhtml").write_text(mhtml)
    assert page_text(tmp_path / "p.mhtml", "mhtml").startswith("Wielki skandal")

    small = "<html><body><p>Przekierowanie</p></body></html>"
    har = {"log": {"entries": [
        {"response": {"content": {"mimeType": "text/html", "text": small}}},
        {"response": {"content": {"mimeType": "application/javascript", "text": "alert(1)"}}},
        {"response": {"content": {"mimeType": "text/html; charset=utf-8", "encoding": "base64",
                                  "text": base64.b64encode(HTML.encode()).decode()}}},
    ]}}
    (tmp_path / "p.har").write_text(json.dumps(har))
    assert page_text(tmp_path / "p.har", "har").startswith("Wielki skandal")
    (tmp_path / "odd.har").write_text(json.dumps({"log": {"entries": [None, 3, {"response": "x"},
        {"response": {"content": {"mimeType": "text/html", "text": HTML}}}]}}))
    assert page_text(tmp_path / "odd.har", "har").startswith("Wielki skandal")
    (tmp_path / "bad.har").write_text("not json")
    assert page_text(tmp_path / "bad.har", "har") == ""


def _no_embeddings(monkeypatch):
    import ftf.features.text as text

    monkeypatch.setattr(text._Embeddings, "__call__", lambda self, t: None)


def test_apply_page_text_and_template(tmp_path, monkeypatch):
    pytest.importorskip("bs4")
    pytest.importorskip("lxml")
    _no_embeddings(monkeypatch)
    raw = tmp_path / "raw"
    for name, body in (("a", A), ("b", B), ("x", OTHER)):
        (raw / name).mkdir(parents=True)
        (raw / name / "page.html").write_text(f"<html><body><p>{body}</p></body></html>")
    ds = dataset_from_files(sorted(raw.rglob("*.html")), raw)
    out = tmp_path / "out"
    assert "text" in pipeline.available_families()
    ds = pipeline.apply_all(ds, raw, out, ["text"])
    write_dataset(ds, out)
    kinds = [i.kind for inc in ds.incidents for i in inc.indicators]
    assert kinds.count("page_text") == 3 and kinds.count("text_template") == 3
    assert "person_mention" in kinds and "keyword" in kinds and "topic" in kinds
    tmpl = {}
    for inc in ds.incidents:
        page = next(i for i in inc.indicators if i.kind == "page_text")
        t = next(i for i in inc.indicators if i.kind == "text_template")
        assert t.provenance.locator == f"src={page.id}" and t.extractor.startswith("text@")
        tmpl[inc.source_ref.split("/")[0]] = t.value
    assert tpl.estimate(tmpl["a"], tmpl["b"]) == 1.0
    assert tpl.estimate(tmpl["a"], tmpl["x"]) < 0.1
    masked = json.loads((out / "text" / "masked.json").read_text())
    assert len(masked) == 3
    assert sum("<PERSON>" in v["masked_text"] for v in masked.values()) == 2
    stats = json.loads((out / "text" / "stats.json").read_text())
    assert stats["page_items"] == 3 and not stats["errors"]

    from ftf.features.text.report import main

    assert main([str(out), "--pair", "a/", "b/"]) == 0

    # Re-running replaces the family's indicators instead of duplicating them.
    ds = pipeline.apply_all(ds, raw, out, ["text"])
    assert sum(i.kind == "page_text" for inc in ds.incidents for i in inc.indicators) == 3


def test_ocr_screenshot_yields_text(tmp_path, monkeypatch):
    PIL = pytest.importorskip("PIL")
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont

    _no_embeddings(monkeypatch)
    raw = tmp_path / "raw"
    (raw / "c").mkdir(parents=True)
    img = Image.new("RGB", (1400, 300), "white")
    font = ImageFont.load_default(size=48)
    ImageDraw.Draw(img).text((40, 40), "Pilne! Zarobił 1 234 zł na inwestycji", fill="black", font=font)
    ImageDraw.Draw(img).text((40, 150), "Sponsorowane", fill="black", font=font)
    img.save(raw / "c" / "screenshot.png")
    ds = dataset_from_files([raw / "c" / "screenshot.png"], raw)
    ds = pipeline.apply_all(ds, raw, tmp_path / "out", ["text"])
    ocr = [i for i in ds.incidents[0].indicators if i.kind == "ocr_text"]
    assert len(ocr) == 1 and "Zarobi" in ocr[0].value and PIL
    assert any(i.kind == "keyword" for i in ds.incidents[0].indicators)


@pytest.mark.skipif(
    not __import__("os").environ.get("FTF_TEST_MODELS"), reason="set FTF_TEST_MODELS=1 to download models"
)
def test_embedding_model_runs():
    pytest.importorskip("onnxruntime")
    from ftf.features.text.embed import get_embedder, spec

    e = get_embedder(threads=2)
    a, b, c = (e.embed(t) for t in (A, B, OTHER))
    assert a.shape == (spec().dim,) and abs(float(np.linalg.norm(a)) - 1) < 1e-4
    assert float(a @ b) > float(a @ c)


def test_shared_surname_needs_first_name():
    assert mask("Anna Lewandowska poleca dietę.").people == ["Anna Lewandowska"]
    assert mask("Robertem Lewandowskim zachwycony.").people == ["Robert Lewandowski"]
    m = mask("Lewandowska poleca dietę.")
    assert m.people == [] and "<PERSON>" in m.text
    assert mask("Hołowni gratulowano.").people == ["Szymon Hołownia"]


def test_caps_headline_masks_names():
    assert mask("ROBERT LEWANDOWSKI ZDRADZA SEKRET").people == ["Robert Lewandowski"]
    assert mask("Szary lis biegnie przez las").people == []


def test_outlet_domains_mask_like_outlet_names():
    assert mask("Czytaj na Onet.pl").text == mask("Czytaj na Onet").text == "Czytaj na <OUTLET>"
    assert mask("wejdź na sklep.example.com/x").text == "wejdź na <URL>"


def test_ocr_chrome_filter_keeps_ad_copy():
    from ftf.features.text.ocr import content_lines

    lines = [
        "Szczegóły reklamy x",
        "Identyfikator biblioteki: 1234567890123456",
        "Poznaj szczegóły Learn More",
        "Reklama ma kilka wersji. © - eae",
        "Zarejestruj się teraz i odbierz 3000 zł",
        "Platformy AI zmieniają wszystko",
        "Ten sposób zmienił moje życie",
    ]
    assert content_lines(lines) == lines[4:]
    assert content_lines(["To już koniec?", "- " * 4000 + "Kliknij", "…" * 5000 + " x"]) == [
        "To już koniec?", "- " * 4000 + "Kliknij", "…" * 5000 + " x"
    ]  # no chrome phrase: kept, and long symbol runs are handled in linear time
    assert content_lines(["Sponsorowane " + "· " * 5000]) == []


def test_short_masked_text_gets_no_template():
    from ftf.features.text import derived_features

    feats, masked = derived_features("Iga Świątek 12.05.2024 1000 zł")
    assert masked == "<PERSON> <DATE> <PRICE>"
    assert "text_template" not in [f.kind for f in feats]
    assert tpl.shingles(tokens(masked)) == set()
    with pytest.raises(ValueError):
        tpl.minhash(set())
    assert kw.keywords("Stawka w Bankier.pl rośnie, potencjał ai") == []


def test_ocr_timeout_env(monkeypatch):
    from ftf.features.text.ocr import ocr_timeout

    monkeypatch.delenv("FTF_OCR_TIMEOUT", raising=False)
    assert ocr_timeout() == 600
    monkeypatch.setenv("FTF_OCR_TIMEOUT", "7")
    assert ocr_timeout() == 7


def test_ocr_timeout_is_skipped_with_warning_and_other_families_run(tmp_path, monkeypatch, caplog):
    import subprocess

    pytest.importorskip("bs4")
    pytest.importorskip("lxml")
    import ftf.features.text.ocr as ocr

    _no_embeddings(monkeypatch)

    def slow(path, min_conf=ocr.MIN_LINE_CONF):
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=1)

    monkeypatch.setattr(ocr, "ocr_lines", slow)
    raw = tmp_path / "raw"
    (raw / "c").mkdir(parents=True)
    pytest.importorskip("PIL").Image.new("RGB", (16, 16), "white").save(raw / "c" / "screenshot.png")
    (raw / "c" / "page.html").write_text(f"<html><body><p>{A}</p></body></html>")
    ds = dataset_from_files(sorted((raw / "c").iterdir()), raw)
    with caplog.at_level("WARNING"):
        ds = pipeline.apply_all(ds, raw, tmp_path / "out", ["text"])
    indicators = [i for inc in ds.incidents for i in inc.indicators]
    skipped = [i for i in indicators if i.kind == "ocr_skipped"]
    assert len(skipped) == 1 and skipped[0].value == "timeout"
    assert skipped[0].layer.value == "collector" and skipped[0].level.value == "capture"
    assert "page_text" in [i.kind for i in indicators] and "text_template" in [i.kind for i in indicators]
    assert len([r for r in caplog.records if "OCR timed out" in r.getMessage()]) == 1
    stats = json.loads((tmp_path / "out" / "text" / "stats.json").read_text())
    assert stats["ocr_timeouts"] == 1
