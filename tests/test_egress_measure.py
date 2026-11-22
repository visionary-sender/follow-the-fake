"""scripts/egress_measure.py on canned manifests and scanner summaries (no network, no browser)."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "egress_measure.py"
_spec = importlib.util.spec_from_file_location("egress_measure", SCRIPT)
em = importlib.util.module_from_spec(_spec)
sys.modules["egress_measure"] = em
_spec.loader.exec_module(em)


def _row(url, outcome, source="certpl"):
    return {"target_url": url, "fetch_outcome": outcome, "source": source, "incident_id": f"x:{url}"}


def test_norm_url_drops_only_fbclid():
    assert em.norm_url("https://a.example/p?x=1&fbclid=abc") == "https://a.example/p?x=1"
    assert em.norm_url("https://a.example/p") == "https://a.example/p"


def test_cloaked_targets_dedups_and_marks_meta():
    rows = [_row("https://a.example/1", "bot_wall"), _row("https://a.example/1", "decoy", "meta"),
            _row("https://b.example/2", "live"), _row("https://c.example/3", "empty"), _row(None, "decoy"),
            _row("ftp://d.example/4", "decoy")]
    t = em.cloaked_targets(rows)
    assert [x["url"] for x in t] == ["https://a.example/1", "https://c.example/3"]
    assert t[0]["meta"] is True and t[1]["meta"] is False


def test_lists_shards_by_meta(tmp_path):
    m = tmp_path / "manifest.jsonl"
    rows = [_row(f"https://m{i}.example/", "decoy", "meta") for i in range(5)] + [_row("https://o.example/", "bot_wall")]
    m.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "lists"
    assert em.main(["lists", "--manifest", str(m), "--out", str(out), "--shards", "2"]) == 0
    meta = (out / "landers-meta-1.txt").read_text().split() + (out / "landers-meta-2.txt").read_text().split()
    assert sorted(meta) == sorted(r["target_url"] for r in rows[:5])
    assert (out / "landers-other-1.txt").read_text().split() == ["https://o.example/"]
    assert (out / "landers-other-2.txt").read_text() == ""


def test_probe_row_reads_query_status():
    summary = {"http_requests": 3, "sources": {"google_atc": {"collected": 0, "queries": [
        {"keyword": "invest", "status": "blocked", "ads": 0, "error": "HTTP 429 for https://x"}]}}}
    r = em.probe_row("google_atc", "invest", summary, 1, 2.0, "")
    assert r["status"] == "blocked" and r["ads"] == 0 and "429" in r["error"]
    assert em.probe_row("meta", "placeholder term", None, -1, 1.0, "boom")["status"] == "no_summary"


def test_last_json_takes_the_final_line():
    assert em._last_json('log\n{"a": 1}\n{"run_id": "r"}\n') == {"run_id": "r"}
    assert em._last_json("no json") is None


def test_report_joins_egress_and_baseline(tmp_path):
    m = tmp_path / "manifest.jsonl"
    m.write_text("".join(json.dumps(r) + "\n" for r in [
        _row("https://a.example/1", "decoy", "meta"), _row("https://b.example/2", "bot_wall"), _row("https://c.example/3", "empty")]))

    def cap(d, url, outcome, egress=None, profile="victim-pl-mobile"):
        p = d / url.split("//")[1].replace("/", "_")
        p.mkdir(parents=True)
        (p / "capture.json").write_text(json.dumps({"requested_url": url, "outcome": outcome, "started_at": "2000-01-01T00:00:00Z",
                                                    "profile": {"name": profile}, "egress": egress}))

    eg = {"zone": "europe-central2-a", "ip_hash": "sha256:" + "ab" * 16}
    run = tmp_path / "run"
    cap(run / "w1" / "landers-meta", "https://a.example/1", "live", eg)
    cap(run / "w1" / "landers-other", "https://b.example/2", "bot_wall", eg)
    base = tmp_path / "base"
    cap(base, "https://a.example/1", "decoy")
    cap(base, "https://b.example/2", "live")
    cap(base / "default", "https://c.example/3", "live", profile="default")  # other profile: ignored
    (run / "w1" / "probe.jsonl").write_text(json.dumps({"source": "google_atc", "keyword": "invest", "status": "ok", "ads": 7, "egress": eg}) + "\n")
    assert em.main(["report", "--egress", str(run), "--manifest", str(m), "--baseline", str(base)]) == 0
    rep = json.loads((run / "report.json").read_text())
    L = rep["landers"]
    assert (L["targets"], L["refetched"], L["egress_live"]) == (3, 2, 1)
    assert (L["compared_with_sandbox_victim"], L["sandbox_victim_live"], L["only_egress_live"], L["only_sandbox_live"]) == (2, 1, 1, 1)
    assert rep["probes"]["per_source"]["google_atc"]["ads"] == 7
    assert "| google_atc | 1 | 0 (0%)" in (run / "report.md").read_text()
