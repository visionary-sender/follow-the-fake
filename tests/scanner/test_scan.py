"""Scan orchestration: contract output, queue deduplication across runs, caps, lock, CLI."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from ftf.cli import main
from ftf.contract import ArtefactType, Dataset, Layer, Stage
from ftf.scanner import run as scan_run
from ftf.scanner.records import AdRecord
from ftf.scanner.store import Locked, Store, load_incident


@dataclass
class FakeResult:
    keyword: str
    records: list = field(default_factory=list)
    status: str = "ok"
    evidence: dict = field(default_factory=dict)


def meta_rec(i, **kw):
    return AdRecord(
        source="meta", ad_id=str(1000 + i), keyword="kw", page_id="p1", page_name="Page",
        body=f"text {i}", landing_url=f"https://land{i}.example.test/x", creative_urls=["https://x.fbcdn.net/a.jpg"],
        start_date="2026-11-20", platforms=["FACEBOOK"], **kw,
    )


def fake_source(batches):
    """Collector returning the next batch of records on each call."""
    it = iter(batches)

    def collect(kws, since, max_ads, max_per_keyword, seen):
        return [FakeResult(kws[0] if kws else "*", list(next(it)))]

    return collect


def dataset(root):
    return Dataset.from_json((root / "dataset.json").read_text())


def run(tmp_path, collectors, sources=("meta",), when="2026-11-21T06:00:00", **kw):
    now = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
    return scan_run.scan(tmp_path, list(sources), now=now, collectors=collectors, **kw)


def test_first_run_writes_contract_incidents_and_queue(tmp_path):
    summary = run(tmp_path, {"meta": fake_source([[meta_rec(1), meta_rec(2)]])})
    assert summary["new"] == 2 and summary["queue_length"] == 2
    cap_id = "20261121T060000Z-meta-1001"  # folder
    inc = load_incident(tmp_path / cap_id)
    assert inc == dataset(tmp_path).incidents[0] and len(dataset(tmp_path).incidents) == 2
    assert inc.id == "adlib:1001" and inc.source == "scanner_meta" and inc.source_ref == "1001"
    cap = inc.captures[0]
    assert cap.id == "adlib:1001:ad"
    manifest = json.loads((tmp_path / cap_id / "capture.json").read_text())
    assert manifest["capture_id"] == cap_id and manifest["tool"].startswith("ftf-scanner@")
    assert manifest["contract_version"] and manifest["manifest_version"] == 1
    ad = json.loads((tmp_path / cap_id / "ad.json").read_text())
    assert ad["page_id"] == "p1" and ad["ad_id"] == "1001" and ad["ad_archive_id"] == "1001"
    assert ad["stable_id"] == "adlib:1001" and ad["source"] == "meta" and ad["kind"] == "ad"
    assert manifest["stable_id"] == "adlib:1001" and ad["landing_domain"] == "land1.example.test"
    assert ad["creative_urls"] == ["https://x.fbcdn.net/a.jpg"]
    assert cap.stage is Stage.ad and cap.captured_on == "2026-11-21"
    assert [a.type for a in cap.artefacts] == [ArtefactType.ad_library]
    kinds = {i.kind: i for i in inc.indicators}
    assert kinds["advertiser_page_id"].value == "p1"
    assert kinds["advertiser_page_id"].layer is Layer.distribution_account
    assert kinds["landing_url"].value == "https://land1.example.test/x"
    assert kinds["landing_domain"].value == "land1.example.test"
    assert kinds["ad_text"].value == "text 1"
    assert kinds["scan_keyword"].layer is Layer.collector
    queue = [json.loads(l) for l in (tmp_path / "queue.jsonl").read_text().splitlines()]
    assert [q["key"] for q in queue] == ["meta:1001", "meta:1002"]
    assert queue[0]["status"] == "pending" and queue[0]["landing_domain"] == "land1.example.test"
    assert queue[0]["capture_dir"] == cap_id and queue[0]["incident_id"] == "adlib:1001"
    assert summary["incidents"][0] == (cap_id, inc)
    assert json.loads((tmp_path / "state" / "seen.json").read_text()) == {"meta:1001": summary["run_id"], "meta:1002": summary["run_id"]}


def test_second_run_queues_only_new_ads(tmp_path):
    src = fake_source([[meta_rec(1), meta_rec(2)], [meta_rec(2), meta_rec(3), meta_rec(3)]])
    run(tmp_path, {"meta": src})
    s2 = run(tmp_path, {"meta": src}, when="2026-11-21T12:00:00")
    assert s2["new"] == 1 and s2["queue_length"] == 3
    assert s2["sources"]["meta"]["collected"] == 3 and s2["sources"]["meta"]["new"] == 1
    assert [i.source_ref for _, i in s2["incidents"]] == ["1003"]
    assert [i.source_ref for i in dataset(tmp_path).incidents] == ["1001", "1002", "1003"]


def test_run_without_new_ads_writes_no_incidents(tmp_path):
    src = fake_source([[meta_rec(1)], [meta_rec(1)]])
    run(tmp_path, {"meta": src})
    s2 = run(tmp_path, {"meta": src}, when="2026-11-21T12:00:00")
    assert s2["new"] == 0
    assert s2["incidents"] == [] and len(dataset(tmp_path).incidents) == 1
    assert (tmp_path / "runs" / s2["run_id"] / "summary.json").exists()


def test_queue_survives_lost_state_file(tmp_path):
    src = fake_source([[meta_rec(1)], [meta_rec(1), meta_rec(2)]])
    run(tmp_path, {"meta": src})
    (tmp_path / "state" / "seen.json").unlink()  # crash after the queue append
    s2 = run(tmp_path, {"meta": src}, when="2026-11-21T12:00:00")
    assert s2["new"] == 1 and s2["queue_length"] == 2


def test_hard_cap_per_run(tmp_path):
    src = fake_source([[meta_rec(i) for i in range(10)]])
    s = run(tmp_path, {"meta": src}, max_ads=4)
    assert s["new"] == 4 and s["sources"]["meta"]["new"] == 4


def test_sources_dedupe_independently(tmp_path):
    cert = AdRecord(source="certpl", ad_id="x.test", register_id="5000001", landing_domain="x.test", start_date="2026-11-20", status="listed")
    s = run(tmp_path, {"meta": fake_source([[meta_rec(1)]]), "certpl": fake_source([[cert]])}, sources=("meta", "certpl"))
    assert s["new"] == 2
    c = next(i for i in dataset(tmp_path).incidents if i.source == "scanner_certpl")
    assert c.id == "certpl:x.test" and c.captures[0].id == "certpl:x.test:listing" and c.captures[0].stage is Stage.unknown
    assert c.captures[0].artefacts[0].type is ArtefactType.url
    assert {i.kind for i in c.indicators} >= {"cert_register_id", "landing_domain", "ad_start_date"}


def test_failed_source_does_not_stop_others(tmp_path):
    def broken(kws, since, max_ads, max_per_keyword, seen):
        return [FakeResult("kw", [], "blocked", {"http_status": 403})]

    s = run(tmp_path, {"meta": broken, "certpl": fake_source([[meta_rec(1)]])}, sources=("meta", "certpl"))
    assert s["sources"]["meta"]["queries"][0]["status"] == "blocked"
    assert s["sources"]["meta"]["queries"][0]["http_status"] == 403
    assert s["new"] == 1


def test_lock_blocks_second_scan(tmp_path):
    with Store(tmp_path):
        with pytest.raises(Locked):
            run(tmp_path, {"meta": fake_source([[meta_rec(1)]])})


def test_creative_download_adds_asset_artefacts(tmp_path, monkeypatch):
    from ftf.contract import Artefact
    from ftf.scanner import creatives

    def fake_download(urls, out_root, folder, capture_id, client, limit=4):
        return [Artefact(id=f"{capture_id}:creative-1", type=ArtefactType.asset, path=f"{folder}/creative-1.jpg", sha256="0" * 64, size=3)], []

    monkeypatch.setattr(creatives, "download", fake_download)
    s = run(tmp_path, {"meta": fake_source([[meta_rec(1)]])}, download_creatives=True)
    assert [a.type for a in dataset(tmp_path).incidents[0].captures[0].artefacts] == [ArtefactType.ad_library, ArtefactType.asset]


def test_creatives_only_from_allowed_https_hosts():
    from ftf.scanner.creatives import allowed

    assert allowed("https://scontent.xx.fbcdn.net/v/a.jpg")
    assert allowed("https://tpc.googlesyndication.com/archive/simgad/1")
    assert not allowed("http://scontent.xx.fbcdn.net/v/a.jpg")
    assert not allowed("https://fbcdn.net.evil.test/a.jpg")
    assert not allowed("https://land.example.test/a.jpg")


def test_cli_scan_parses_and_exits_zero(tmp_path, monkeypatch, capsys):
    seen_args = {}

    def fake_scan(out, sources, **kw):
        seen_args.update(kw, out=out, sources=sources)
        return {"run_id": "r", "new": 0, "sources": {"certpl": {"collected": 0, "new": 0, "queries": [{"status": "empty"}]}}}

    monkeypatch.setattr(scan_run, "scan", fake_scan)
    code = main(["scan", "--source", "certpl", "--source", "certpl", "--keywords", "a, b,", "--since", "2026-11-14", "--out", str(tmp_path), "--max-ads", "5"])
    assert code == 0
    assert seen_args["sources"] == ["certpl"] and seen_args["keywords"] == ["a", "b"]
    assert str(seen_args["since"]) == "2026-11-14" and seen_args["max_ads"] == 5
    assert json.loads(capsys.readouterr().out)["sources"]["certpl"]["statuses"] == ["empty"]


def test_cli_scan_fails_when_every_source_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan_run, "scan",
        lambda out, sources, **kw: {"run_id": "r", "new": 0, "sources": {"meta": {"collected": 0, "new": 0, "queries": [{"status": "blocked"}]}}},
    )
    assert main(["scan", "--source", "meta", "--out", str(tmp_path)]) == 1


def test_cli_rejects_bad_date(capsys):
    with pytest.raises(SystemExit):
        main(["scan", "--since", "21-11-2026"])


def test_first_source_is_saved_when_a_later_one_crashes(tmp_path):
    def crash(*a):
        raise RuntimeError("browser died")

    with pytest.raises(RuntimeError):
        run(tmp_path, {"certpl": fake_source([[meta_rec(1)]]), "meta": crash}, sources=("certpl", "meta"))
    assert Store(tmp_path).read_queue()[0]["key"] == "meta:1001"
    assert "meta:1001" in json.loads((tmp_path / "state" / "seen.json").read_text())


def test_collectors_get_seen_keys_to_skip(tmp_path):
    got = []

    def collect(kws, since, max_ads, max_per_keyword, skip):
        got.append(set(skip))
        return [FakeResult("kw", [meta_rec(2)])]

    run(tmp_path, {"meta": fake_source([[meta_rec(1)]])})
    run(tmp_path, {"meta": collect}, when="2026-11-21T12:00:00")
    assert got == [{"meta:1001"}]


def test_creative_budget_caps_downloads(tmp_path, monkeypatch):
    from ftf.scanner import creatives

    limits = []

    def fake_download(urls, out_root, folder, capture_id, client, limit=4):
        limits.append(limit)
        return [], []

    monkeypatch.setattr(creatives, "download", fake_download)
    recs = [meta_rec(i) for i in range(5)]
    for r in recs:
        r.creative_urls = ["https://a.fbcdn.net/1.jpg", "https://a.fbcdn.net/2.jpg"]
    run(tmp_path, {"meta": fake_source([recs])}, download_creatives=True, max_creatives=3)
    assert limits == [2, 1]


def test_cli_rejects_zero_cap():
    with pytest.raises(SystemExit):
        main(["scan", "--max-ads", "0"])


def test_library_api_scan_queue_and_mark(tmp_path, monkeypatch):
    from ftf import scanner

    real = scan_run.scan
    monkeypatch.setattr(
        scan_run, "scan",
        lambda out, sources, **kw: real(out, sources, collectors={"certpl": fake_source([[meta_rec(1), meta_rec(2)]])}, **kw),
    )
    ads = scanner.scan(["certpl"], since=None, out=tmp_path)
    assert [a.ad_id for a in ads] == ["1001", "1002"]
    assert ads[0].capture_dir == tmp_path / ads[0].capture_id and ads[0].ad["page_id"] == "p1"
    assert ads[0].incident == load_incident(ads[0].capture_dir) and ads[0].key == "meta:1001" and ads[0].stable_id == "adlib:1001"
    assert [q["key"] for q in scanner.pending(tmp_path)] == ["meta:1001", "meta:1002"]
    assert scanner.mark(tmp_path, ["meta:1001"], "assigned") == 1
    assert scanner.mark(tmp_path, ["meta:1001"], "assigned") == 0
    assert [q["key"] for q in scanner.pending(tmp_path)] == ["meta:1002"]
    assert scanner.queue(tmp_path)[0]["status"] == "assigned"
    with pytest.raises(ValueError):
        scanner.mark(tmp_path, ["meta:1002"], "done")
    with pytest.raises(ValueError):
        scanner.scan(["tiktok"], out=tmp_path)


def test_mark_waits_for_lock_then_gives_up(tmp_path):
    from ftf import scanner

    run(tmp_path, {"meta": fake_source([[meta_rec(1)]])})
    with Store(tmp_path):
        with pytest.raises(Locked):
            scanner.mark(tmp_path, ["meta:1001"], "assigned", wait=0)
    assert scanner.mark(tmp_path, ["meta:1001"], "assigned", wait=0) == 1


def test_unreadable_dataset_is_set_aside(tmp_path):
    (tmp_path / "dataset.json").write_text('{"contract_version": "9.0.0", "name": "old"}')
    s = run(tmp_path, {"meta": fake_source([[meta_rec(1)]])})
    assert s["new"] == 1 and len(dataset(tmp_path).incidents) == 1
    assert list(tmp_path.glob("dataset.invalid-*.json"))


def test_cli_fails_when_only_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan_run, "scan",
        lambda out, sources, **kw: {"run_id": "r", "new": 0, "sources": {"google_atc": {"collected": 0, "new": 0, "queries": [{"status": "blocked"}, {"status": "skipped"}]}}},
    )
    assert main(["scan", "--source", "google_atc", "--out", str(tmp_path)]) == 1
