"""Fetcher (HACK-17) and scanner (HACK-20) capture folders; synthetic, example.test only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ftf.cli import main as cli_main
from ftf.contract import ArtefactType, Dataset, Incident, Layer, Observed, Stage, linkable
from ftf.ingest import ingest, load_inputs
from ftf.ingest.adlib import load_ad
from ftf.ingest.manifests import artefact_file, safe_parts

from .conftest import write
from .test_ingest import fetcher_capture

STAMP = "20261121T224558Z"


def scan_capture(out: Path, ad_id: str, *, source: str = "meta", stamp: str = STAMP, creatives: tuple[str, ...] = ("creative-1.jpg",),
                 ad: dict | None = None, finished: str = "2026-11-21T22:46:00Z", extra_paths: dict | None = None) -> Path:
    folder = f"{stamp}-{source}-{ad_id}"
    d = out / folder
    certpl = source == "certpl"
    inc_id = f"certpl:{ad_id}" if certpl else f"adlib:{ad_id}"
    cap_id = f"{inc_id}:listing" if certpl else f"{inc_id}:ad"
    record = {
        "source": source, "kind": "domain" if certpl else "ad", "stable_id": inc_id, "ad_archive_id": None if certpl else ad_id,
        "ad_id": ad_id, "key": f"{source}:{ad_id}", "keyword": None if certpl else "lek", "run_id": stamp,
        "page_id": None if certpl else "555000111", "page_name": None if certpl else "Zdrowie Example",
        "advertiser_id": None, "advertiser_name": None, "body": None if certpl else "Cudowny lek, kup teraz",
        "title": None, "link_description": None, "display_domain": None if certpl else "shop.example.test",
        "landing_url": None if certpl else "https://shop.example.test/lp?x=1", "declared_link_url": None,
        "landing_domain": ad_id if certpl else "shop.example.test", "creative_urls": [], "collation_id": None,
        "display_format": None, "platforms": [], "start_date": "2026-11-01", "end_date": None if certpl else "2026-11-20",
        "status": "listed" if certpl else "ACTIVE", "register_id": "100001" if certpl else None,
    }
    record.update(ad or {})
    write(d / "ad.json", json.dumps(record))
    arts = [{"id": f"{cap_id}:library", "type": "url" if certpl else "ad_library"}]
    for name in creatives:
        write(d / name, b"\xff\xd8\xff fake jpeg " + name.encode() + ad_id.encode())
        arts.append({"id": f"{cap_id}:{name.rsplit('.', 1)[0]}", "type": "asset", "path": f"{folder}/{name}", "sha256": "0" * 64, "size": 1})
    for aid, path in (extra_paths or {}).items():
        arts.append({"id": f"{cap_id}:{aid}", "type": "asset", "path": path})
    inc = {"id": inc_id, "source": f"scanner_{source}", "source_ref": folder,
           "captures": [{"id": cap_id, "stage": "unknown" if certpl else "ad", "observed": "static", "captured_on": "2026-11-21",
                         "artefacts": arts, "missing": []}]}
    Incident.model_validate(inc)
    write(d / "capture.json", json.dumps({"manifest_version": 1, "contract_version": "0.1.0", "tool": "ftf-scanner@0.1.0",
                                          "capture_id": folder, "source": source, "ad_id": ad_id, "stable_id": inc_id, "run_id": stamp,
                                          "started_at": "2026-11-21T22:45:58Z", "finished_at": finished, "incident": inc}))
    return d


def scanner_run(out: Path) -> Path:
    scan_capture(out, "1000000000000001")
    scan_capture(out, "1000000000000002", creatives=("creative-1.jpg", "creative-2.mp4"))
    scan_capture(out, "scam.example.test", source="certpl", creatives=())
    write(out / "dataset.json", json.dumps({"name": "scan", "incidents": []}))
    write(out / "queue.jsonl", json.dumps({"key": "meta:1000000000000001", "status": "pending"}) + "\n")
    write(out / "state" / "seen.json", json.dumps({"meta:1000000000000001": STAMP}))
    write(out / "runs" / STAMP / "summary.json", json.dumps({"meta": {"status": "ok"}}))
    return out


def caps(ds: Dataset) -> dict:
    return {c.id: c for i in ds.incidents for c in i.captures}


# -- scanner --------------------------------------------------------------------------------------


def test_scanner_run_keeps_ids_and_types(tmp_path):
    run = scanner_run(tmp_path / "scanner")
    ds = load_inputs([run])
    assert sorted(i.id for i in ds.incidents) == ["adlib:1000000000000001", "adlib:1000000000000002", "certpl:scam.example.test"]
    c = caps(ds)
    ad = c["adlib:1000000000000001:ad"]
    assert ad.stage is Stage.ad and ad.observed is Observed.static and ad.captured_on == "2026-11-21"
    types = {a.id: a.type for a in ad.artefacts}
    assert types == {"adlib:1000000000000001:ad:library": ArtefactType.ad_library,
                     "adlib:1000000000000001:ad:creative-1": ArtefactType.ad_screenshot,
                     "adlib:1000000000000001:ad:manifest": ArtefactType.other}
    for a in ad.artefacts:  # re-rooted to the ingest root (the run's parent), re-hashed
        assert a.path.startswith(f"scanner/{STAMP}-meta-1000000000000001/") and a.sha256 != "0" * 64 and a.size > 0
    assert {a.type for a in c["adlib:1000000000000002:ad"].artefacts} == {ArtefactType.ad_library, ArtefactType.ad_screenshot,
                                                                         ArtefactType.video, ArtefactType.other}
    listing = c["certpl:scam.example.test:listing"]
    assert listing.stage is Stage.unknown
    assert {a.type for a in listing.artefacts} == {ArtefactType.ad_library, ArtefactType.other}
    # queue.jsonl, state/ and runs/ are not read
    assert not any("queue" in a.path or "state/" in a.path or "runs/" in a.path for cap in c.values() for a in cap.artefacts)


def test_load_ad_exposes_fields(tmp_path):
    run = scanner_run(tmp_path / "scanner")
    ds = load_inputs([run], root=tmp_path)
    c = caps(ds)
    ad = load_ad(c["adlib:1000000000000001:ad"], tmp_path)
    assert ad["ad_text"] == ad["body"] == "Cudowny lek, kup teraz"
    assert (ad["page_name"], ad["page_id"], ad["landing_url"]) == ("Zdrowie Example", "555000111", "https://shop.example.test/lp?x=1")
    assert (ad["first_date"], ad["last_date"]) == ("2026-11-01", "2026-11-20")
    assert load_ad(c["certpl:scam.example.test:listing"], tmp_path)["kind"] == "domain"
    write(tmp_path / "other" / "x.png", b"\x89PNG")
    (only,) = load_inputs([tmp_path / "other" / "x.png"]).incidents
    assert load_ad(only.captures[0], tmp_path / "other") == {}


def test_single_scan_folder(tmp_path):
    d = scan_capture(tmp_path / "scanner", "1000000000000003")
    (inc,) = load_inputs([d]).incidents
    assert inc.id == "adlib:1000000000000003" and inc.source == "scanner_meta"
    assert [cap.id for cap in inc.captures] == ["adlib:1000000000000003:ad"]


def test_same_ad_in_two_scans_keeps_newest(tmp_path):
    out = tmp_path / "scanner"
    scan_capture(out, "1000000000000004", stamp="20261121T180000Z", ad={"body": "old"}, finished="2026-11-21T18:00:30Z")
    scan_capture(out, "1000000000000004", stamp="20261122T000000Z", ad={"body": "new"}, finished="2026-11-22T00:00:30Z")
    ds = load_inputs([out], root=tmp_path)
    (inc,) = ds.incidents
    assert inc.source_ref == "20261122T000000Z-meta-1000000000000004"
    assert load_ad(inc.captures[0], tmp_path)["body"] == "new"


def test_unlisted_ad_record_and_creatives_are_read(tmp_path):
    d = scan_capture(tmp_path / "scanner", "1000000000000005", creatives=())
    write(d / "creative-1.png", b"\x89PNG unlisted")
    m = json.loads((d / "capture.json").read_text())
    m["incident"]["captures"][0]["artefacts"] = []
    write(d / "capture.json", json.dumps(m))
    (inc,) = load_inputs([d]).incidents
    assert {(a.id, a.type) for a in inc.captures[0].artefacts} == {
        ("adlib:1000000000000005:ad:library", ArtefactType.ad_library),
        ("adlib:1000000000000005:ad:creative-1", ArtefactType.ad_screenshot),
        ("adlib:1000000000000005:ad:manifest", ArtefactType.other),
    }


# -- fetcher --------------------------------------------------------------------------------------


def test_refetch_with_victim_profile_is_one_incident(tmp_path):
    out = tmp_path / "captures"
    first = "20261121T220000Z-pi.example.test-00000001"
    second = "20261121T220100Z-pi.example.test-00000002"
    # the victim-profile fetch sorts first by name; default must still come first
    fetcher_capture(out, "20261121T215900Z-pi.example.test-00000000", "https://pi.example.test/", profile="victim-pl-mobile",
                    started="2026-11-21T22:01:00Z")
    fetcher_capture(out, first, "https://pi.example.test/", started="2026-11-21T22:00:00Z")
    fetcher_capture(out, second, "https://rho.example.test/")
    ds = ingest([out])
    by_id = {i.id: i for i in ds.incidents}
    assert set(by_id) == {f"fetch:{first}", f"fetch:{second}"}
    pi = by_id[f"fetch:{first}"]
    assert [c.id for c in pi.captures] == [f"fetch:{first}:c1", f"fetch:{first}:c2"]
    # artefact ids keep the fetcher's ids
    assert f"fetch:{first}:har" in {a.id for a in pi.captures[0].artefacts}
    assert "fetch:20261121T215900Z-pi.example.test-00000000:har" in {a.id for a in pi.captures[1].artefacts}
    prof = {i.capture_id: i for i in pi.indicators if i.kind == "fetch_profile"}
    assert prof[f"fetch:{first}:c1"].value == "default" and prof[f"fetch:{first}:c2"].value == "victim-pl-mobile"
    for ind in pi.indicators:
        if ind.kind in ("fetch_profile", "fetch_outcome"):
            assert ind.layer is Layer.collector and not linkable(ind)
            assert ind.provenance.artefact_id in {a.id for c in pi.captures if c.id == ind.capture_id for a in c.artefacts}
    Dataset.model_validate(json.loads(ds.to_json()))


def test_fetches_sharing_an_incident_id_merge_and_keep_capture_ids(tmp_path):
    out = tmp_path / "captures"
    a = fetcher_capture(out, "20261121T220000Z-sigma.example.test-00000001", "https://sigma.example.test/")
    b = fetcher_capture(out, "20261121T220100Z-sigma.example.test-00000002", "https://sigma.example.test/?b", profile="victim-pl-mobile")
    m = json.loads((b / "capture.json").read_text())
    inc = m["incident"]
    inc["id"] = "fetch:20261121T220000Z-sigma.example.test-00000001"
    inc["captures"][0]["id"] = f"{inc['id']}:c2"
    inc["indicators"] = []
    write(b / "capture.json", json.dumps(m))
    (merged,) = load_inputs([out]).incidents
    assert merged.id == "fetch:20261121T220000Z-sigma.example.test-00000001"
    assert [c.id for c in merged.captures] == [f"{merged.id}:c1", f"{merged.id}:c2"]
    assert a.name in merged.captures[0].artefacts[0].path


# -- untrusted manifests ----------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x/ad.json", "a/../../ad.json", "a\\b.jpg", "", None, 7])
def test_safe_parts_rejects(bad):
    assert safe_parts(bad) is None


def test_paths_outside_the_folder_are_ignored(tmp_path):
    secret = write(tmp_path / "secret.jpg", b"\xff\xd8 secret")
    d = scan_capture(tmp_path / "scanner", "1000000000000006", creatives=(),
                     extra_paths={"up": "../secret.jpg", "abs": str(secret), "sib": f"{STAMP}-meta-1000000000000006/../../secret.jpg"})
    (d / "creative-9.jpg").symlink_to(secret)
    (inc,) = load_inputs([d], root=tmp_path).incidents
    paths = [a.path for a in inc.captures[0].artefacts]
    assert all("secret" not in p and "creative-9" not in p for p in paths)
    assert artefact_file(d, "creative-9.jpg") is None


def test_unknown_tool_or_version_is_refused(tmp_path, capsys):
    d = fetcher_capture(tmp_path / "captures", "20261121T220000Z-tau.example.test-00000001")
    m = json.loads((d / "capture.json").read_text())
    write(d / "capture.json", json.dumps({**m, "tool": "someone-else@1"}))
    with pytest.raises(ValueError, match="unknown capture tool"):
        load_inputs([d])
    write(d / "capture.json", json.dumps({**m, "manifest_version": 2}))
    with pytest.raises(ValueError, match="manifest_version"):
        load_inputs([d])
    # in a run, a bad folder is skipped and the rest is read
    fetcher_capture(tmp_path / "captures", "20261121T220100Z-upsilon.example.test-00000002", "https://upsilon.example.test/")
    assert [i.id for i in load_inputs([tmp_path / "captures"]).incidents] == ["fetch:20261121T220100Z-upsilon.example.test-00000002"]
    assert cli_main(["ingest", str(d), "--out", str(tmp_path / "d.json")]) == 2


def test_cli_ingests_scanner_run(tmp_path, capsys):
    run = scanner_run(tmp_path / "scanner")
    out = tmp_path / "out" / "dataset.json"
    assert cli_main(["ingest", str(run), "--out", str(out)]) == 0
    ds = Dataset.from_json(out.read_text())
    assert len(ds.incidents) == 3
    assert not any(i.kind == "fetch_profile" for i in ds.indicators())  # scanner manifests carry no fetch profile
