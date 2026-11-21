from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from ftf.cli import main as cli_main
from ftf.contract import ArtefactType, Dataset, Incident, Indicator, Layer, Level, Observed, Stage
from ftf.ingest import ids, ingest, load_inputs, run
from ftf.ingest import adapters

from .conftest import MHTML, har, html, saved_page, write


def caps(ds: Dataset) -> dict:
    return {c.id: c for i in ds.incidents for c in i.captures}


def organiser(raw: Path) -> Dataset:
    return load_inputs([next(raw.glob("do_*")), raw / "BACKUP"], root=raw)


# -- ids -----------------------------------------------------------------------------------------


def test_sanitise_folds_and_collapses():
    assert ids.sanitise("do_udostępnienia") == "do_udostepnienia"
    assert ids.sanitise("Trwały Działunek") == "Trwaly-Dzialunek"
    assert ids.sanitise("«Wiem, kto»  — Gazeta.pl") == "Wiem-kto-Gazeta.pl"
    assert ids.sanitise("--a//b--") == "a-b"
    assert ids.sanitise("Metabo RX") == "Metabo-RX"


def test_id_functions():
    inc = ids.incident_id("do_udostępnienia", "61")
    assert inc == "do_udostepnienia:61"
    cap = ids.capture_id(inc, "strona_1")
    assert cap == "do_udostepnienia:61:strona_1"
    assert ids.artefact_id(cap, "strona_1/37 minut.html") == "do_udostepnienia:61:strona_1:strona_1/37-minut.html"
    assert ids.fetch_incident_id("20261121T220000Z-example.test-1a2b3c4d") == "fetch:20261121T220000Z-example.test-1a2b3c4d"
    assert ids.adlib_incident_id("1234567890") == "adlib:1234567890"
    assert ids.input_incident_id(["b" * 64, "a" * 64]) == ids.input_incident_id(["a" * 64, "b" * 64])
    assert ids.input_incident_id("https://x.example.test/").startswith("input:") and len(ids.input_incident_id("https://x.example.test/")) == 18
    assert ids.canonical(["BACKUP:p2:c1", "BACKUP:p1:x"]) == "BACKUP:p1:x"
    with pytest.raises(ValueError):
        ids.incident_id("BACKUP", "«»")


# -- organiser data ------------------------------------------------------------------------------


def test_organiser_incidents_and_paths(raw):
    ds = organiser(raw)
    assert {i.id for i in ds.incidents} == {
        "do_udostepnienia:1", "do_udostepnienia:2", "do_udostepnienia:5", "do_udostepnienia:61",
        "BACKUP:p1", "BACKUP:p2", "BACKUP:p10",
    }
    inc = next(i for i in ds.incidents if i.id == "do_udostepnienia:1")
    assert inc.source == "do_udostepnienia" and inc.source_ref == "1"
    (cap,) = inc.captures
    assert cap.id == "do_udostepnienia:1:c1" and cap.stage is Stage.lander and cap.observed is Observed.live
    assert cap.captured_on == "2026-10-09"
    types_ = {a.type for a in cap.artefacts}
    assert types_ == {ArtefactType.ad_screenshot, ArtefactType.har, ArtefactType.html, ArtefactType.asset}
    for a in cap.artefacts:
        assert (raw / a.path).is_file() and len(a.sha256) == 64 and a.size == (raw / a.path).stat().st_size
        assert a.path.startswith("do_")
    assert {m.modality for m in cap.missing} == {ArtefactType.video, ArtefactType.ad_library}
    every_file = {p for p in raw.rglob("*") if p.is_file()}
    listed = {raw / a.path for i in ds.incidents for c in i.captures for a in c.artefacts}
    assert listed == every_file


def test_folder_with_two_sites_splits(raw):
    c = caps(organiser(raw))
    beta, gamma = c["do_udostepnienia:2:beta.example.test"], c["do_udostepnienia:2:gamma.example.test"]
    assert {a.type for a in beta.artefacts} == {ArtefactType.har, ArtefactType.html, ArtefactType.asset}
    assert [a.type for a in gamma.artefacts] == [ArtefactType.har]
    assert {m.modality for m in gamma.missing} >= {ArtefactType.html, ArtefactType.ad_screenshot}


def test_case_61_three_page_captures_and_decoy(raw):
    ds = organiser(raw)
    c = caps(ds)
    inc = next(i for i in ds.incidents if i.id == "do_udostepnienia:61")
    assert [x.id for x in inc.captures] == [
        "do_udostepnienia:61:strona_1", "do_udostepnienia:61:strona_2", "do_udostepnienia:61:strona_3", "do_udostepnienia:61:reklama_1",
    ]
    assert c["do_udostepnienia:61:reklama_1"].stage is Stage.ad
    assert c["do_udostepnienia:61:reklama_1"].artefacts[0].type is ArtefactType.ad_screenshot
    assert c["do_udostepnienia:61:strona_1"].stage is Stage.lander
    assert c["do_udostepnienia:61:strona_1"].observed is Observed.static  # local-file HAR
    assert c["do_udostepnienia:61:strona_2"].stage is Stage.lander
    assert c["do_udostepnienia:61:strona_3"].stage is Stage.decoy
    ids_ = {a.id for a in c["do_udostepnienia:61:strona_1"].artefacts}
    assert "do_udostepnienia:61:strona_1:strona_1/.har" in ids_
    assert "do_udostepnienia:61:strona_1:strona_1/37-minut.html" in ids_
    assert not c["do_udostepnienia:61:strona_2"].missing  # html + har present, the ad is its own capture


def test_local_file_har_note_and_no_path_leak(raw):
    result = run([next(raw.glob("do_*"))], root=raw, indicators=False)
    rec = next(r for r in result.captures if r.capture_id == "do_udostepnienia:61:strona_1")
    assert any("file://" in n for n in rec.notes)
    assert "analyst" not in json.dumps(result.timing()) and "analyst" not in result.dataset.to_json()


def test_exact_duplicates_collapse(raw):
    c = caps(organiser(raw))
    assert c["BACKUP:p1:epsilon.example.test"].duplicate_of is None
    assert c["BACKUP:p2:c1"].duplicate_of == "BACKUP:p1:epsilon.example.test"
    archive = c["BACKUP:p1:archive_epsilon.example.test_20261008"]
    assert {a.type for a in archive.artefacts} == {ArtefactType.har, ArtefactType.screenshot, ArtefactType.other, ArtefactType.text}
    assert archive.duplicate_of is None


def test_duplicates_across_inputs(raw, tmp_path):
    other = tmp_path / "copy"
    for p in (raw / "BACKUP" / "p2").rglob("*"):
        if p.is_file():
            write(other / p.relative_to(raw / "BACKUP" / "p2"), p.read_bytes())
    ds = load_inputs([raw / "BACKUP" / "p2", other], root=tmp_path)
    c = caps(ds)
    copy = next(x for x in c.values() if x.id.startswith("input:"))
    assert copy.duplicate_of == "BACKUP:p2:c1"


def test_missing_lists_only_absent(raw):
    ds = organiser(raw)
    for cap in caps(ds).values():
        present = {a.type for a in cap.artefacts}
        assert not present & {m.modality for m in cap.missing}
    c = caps(ds)
    assert {m.modality for m in c["do_udostepnienia:5:c1"].missing} == {ArtefactType.ad_screenshot}
    assert c["do_udostepnienia:5:c1"].stage is Stage.ad
    p10 = {m.modality: m.reason for m in c["BACKUP:p10:c1"].missing}
    assert "deleted" in p10[ArtefactType.video]


def test_ids_stable_across_runs(raw):
    assert organiser(raw).to_json() == organiser(raw).to_json()


def test_single_case_folder_and_default_root(raw):
    ds = load_inputs([next(raw.glob("do_*")) / "61"])
    assert ds.incidents[0].id == "do_udostepnienia:61"
    assert all(a.path.startswith("do_") for c in ds.incidents[0].captures for a in c.artefacts)


def test_raw_root_itself(raw):
    assert {i.id for i in load_inputs([raw]).incidents} == {i.id for i in organiser(raw).incidents}


def test_outside_root_is_an_error(raw, tmp_path):
    with pytest.raises(ValueError):
        load_inputs([raw / "BACKUP"], root=tmp_path / "elsewhere")


# -- single files and URLs -----------------------------------------------------------------------


def test_single_har(tmp_path):
    p = write(tmp_path / "in" / "theta.example.test.har", har("https://theta.example.test/x"))
    ds = load_inputs([p])
    (inc,) = ds.incidents
    (cap,) = inc.captures
    assert inc.id == ids.input_incident_id([cap.artefacts[0].sha256])
    assert cap.id == f"{inc.id}:c1" and cap.stage is Stage.lander and cap.observed is Observed.live
    assert cap.artefacts[0].path == "theta.example.test.har"
    assert {m.modality for m in cap.missing} == {ArtefactType.html, ArtefactType.ad_screenshot, ArtefactType.video, ArtefactType.ad_library}


def test_single_html_with_resources(tmp_path):
    saved_page(tmp_path / "in", "Offer page", "https://iota.example.test/")
    ds = load_inputs([tmp_path / "in" / "Offer page.html"])
    (cap,) = ds.incidents[0].captures
    assert sorted(a.type.value for a in cap.artefacts) == ["asset", "asset", "html"]
    assert cap.observed is Observed.static
    assert any(a.id.endswith(":Offer-page_files/main.js.pobrane") for a in cap.artefacts)


def test_single_mhtml(tmp_path):
    p = write(tmp_path / "page.mhtml", MHTML)
    (cap,) = load_inputs([p]).incidents[0].captures
    assert [a.type for a in cap.artefacts] == [ArtefactType.mhtml]
    assert cap.stage is Stage.lander and cap.captured_on == "2026-10-12"


def test_single_screenshot_is_ad(tmp_path):
    p = write(tmp_path / "ad.png", b"\x89PNG solo")
    (cap,) = load_inputs([p]).incidents[0].captures
    assert cap.stage is Stage.ad and cap.artefacts[0].type is ArtefactType.ad_screenshot


def test_bare_url_not_fetched():
    url = "https://kappa.example.test/?access_token=EAAsecretsecretsecretsecret&x=1"
    ds = load_inputs([url])
    (inc,) = ds.incidents
    assert inc.id == ids.input_incident_id(url)
    assert "EAAsecret" not in ds.to_json()
    (cap,) = inc.captures
    assert cap.stage is Stage.unknown and cap.artefacts[0].type is ArtefactType.url and cap.artefacts[0].path is None
    assert {(m.modality, m.reason) for m in cap.missing} == {(t, "not fetched") for t in (ArtefactType.html, ArtefactType.har, ArtefactType.screenshot)}


# -- fetcher folders -----------------------------------------------------------------------------


def fetcher_capture(out: Path, cid: str, url: str = "https://lambda.example.test/", ad: dict | None = None) -> Path:
    d = out / cid
    write(d / "url.txt", url + "\n")
    write(d / "page.har", har(url, "2026-11-21T22:00:00.000Z"))
    write(d / "page.html", html(None))
    arts = []
    for name, t in (("url.txt", "url"), ("page.har", "har"), ("page.html", "html")):
        arts.append({"id": f"fetch:{cid}:{t}", "type": t, "path": f"{cid}/{name}", "sha256": "0" * 64, "size": 1})
    inc = {
        "id": f"fetch:{cid}", "source": "live_fetch", "source_ref": cid,
        "captures": [{"id": f"fetch:{cid}:c1", "stage": "unknown", "observed": "live", "captured_on": "2026-11-21", "artefacts": arts,
                      "missing": [{"modality": "screenshot", "reason": "timeout: slow"}]}],
        "indicators": [{"id": f"fetch:{cid}:fetch_outcome", "capture_id": f"fetch:{cid}:c1", "layer": "collector", "kind": "fetch_outcome",
                        "level": "capture", "value": "timeout", "observed": "live", "provenance": {"artefact_id": f"fetch:{cid}:url"}, "extractor": "ftf-fetch@0.1.0"}],
    }
    Incident.model_validate(inc)
    write(d / "capture.json", json.dumps({"manifest_version": 1, "capture_id": cid, "incident": inc}))
    if ad is not None:
        write(d / "ad.json", json.dumps(ad))
        write(d / "creative.png", b"\x89PNG creative")
    return d


def test_fetcher_folder_keeps_ids(tmp_path):
    cid = "20261121T220000Z-lambda.example.test-1a2b3c4d"
    d = fetcher_capture(tmp_path / "captures", cid)
    ds = load_inputs([d])
    (inc,) = ds.incidents
    assert inc.id == f"fetch:{cid}" and inc.source == "live_fetch" and not inc.indicators
    (cap,) = inc.captures
    assert cap.id == f"fetch:{cid}:c1" and cap.observed is Observed.live
    assert {a.id for a in cap.artefacts} == {f"fetch:{cid}:url", f"fetch:{cid}:har", f"fetch:{cid}:html"}
    for a in cap.artefacts:
        assert a.path.startswith(f"{cid}/") and a.sha256 != "0" * 64  # re-hashed from the files
    assert [m.modality for m in cap.missing] == [ArtefactType.screenshot]
    # a fetcher output root with several captures
    fetcher_capture(tmp_path / "captures", "20261121T220100Z-mu.example.test-00000000", "https://mu.example.test/")
    assert len(load_inputs([tmp_path / "captures"]).incidents) == 2


def test_fetcher_folder_with_ad_record_hook(tmp_path):
    cid = "20261121T220000Z-lambda.example.test-1a2b3c4d"
    d = fetcher_capture(tmp_path / "captures", cid, ad={"ad_archive_id": "1234567890"})
    (inc,) = load_inputs([d]).incidents
    assert inc.id == "adlib:1234567890" and inc.source == "ad_library"
    c = {x.id: x for x in inc.captures}
    assert set(c) == {"adlib:1234567890:ad", "adlib:1234567890:lander"}
    assert {a.type for a in c["adlib:1234567890:ad"].artefacts} == {ArtefactType.ad_library, ArtefactType.ad_screenshot}
    assert c["adlib:1234567890:lander"].stage is Stage.lander


def test_fetch_option_uses_fetcher(tmp_path):
    calls = []

    def fake_fetch(url, out_root):
        calls.append(url)
        d = fetcher_capture(Path(out_root), "20261121T220000Z-nu.example.test-11111111", url)
        return types.SimpleNamespace(capture_dir=d)

    result = run(["https://nu.example.test/"], indicators=False, fetch=True, fetch_dir=tmp_path / "fetched", fetcher=fake_fetch)
    assert calls == ["https://nu.example.test/"]
    assert result.dataset.incidents[0].id == "fetch:20261121T220000Z-nu.example.test-11111111"


# -- indicators, timing and CLI ------------------------------------------------------------------


def _fake_extractor():
    def extract(incident, capture, root):
        a = capture.artefacts[0]
        assert (root / a.path).is_file()
        yield Indicator(id="x", capture_id=capture.id, layer=Layer.landing, kind="first_artefact", level=Level.capture,
                        value=a.sha256, observed=Observed.static, provenance={"artefact_id": a.id}, extractor="fake@0")

    return types.SimpleNamespace(__name__="fake", extract=extract)


def test_ingest_matches_extract_all(raw, monkeypatch):
    import ftf.indicators as ind

    monkeypatch.setattr(ind, "extractors", lambda: [_fake_extractor()])
    ds = ingest([next(raw.glob("do_*")), raw / "BACKUP"], root=raw)
    expected = ind.extract_all(organiser(raw), raw)
    assert ds.to_json() == expected.to_json()
    n_canonical = sum(1 for c in caps(ds).values() if not c.duplicate_of)
    assert len(ds.indicators()) == n_canonical


def test_cli(raw, tmp_path, capsys):
    out, timing = tmp_path / "o" / "dataset.json", tmp_path / "o" / "timing.json"
    code = cli_main(["ingest", str(next(raw.glob("do_*"))), str(raw / "BACKUP"), "--root", str(raw), "--out", str(out), "--timing", str(timing), "--name", "organiser"])
    assert code == 0
    ds = Dataset.from_json(out.read_text())
    assert ds.name == "organiser" and len(ds.incidents) == 7
    t = json.loads(timing.read_text())
    assert t["summary"]["captures"] == len(t["captures"]) == sum(len(i.captures) for i in ds.incidents)
    assert t["summary"]["duplicates"] == 1
    err = capsys.readouterr().err
    assert "7 incidents" in err and "1 duplicates" in err and "s/capture mean" in err


def test_cli_fetch_without_fetcher(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(adapters, "fetch_available", lambda: False)
    code = cli_main(["ingest", "https://xi.example.test/", "--fetch", "--out", str(tmp_path / "d.json")])
    assert code == 2 and "not installed" in capsys.readouterr().err


def test_cli_missing_input(tmp_path, capsys):
    assert cli_main(["ingest", str(tmp_path / "nope"), "--out", str(tmp_path / "d.json")]) == 2
