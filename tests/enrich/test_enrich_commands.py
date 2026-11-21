import json

import pytest

from ftf.cli import main
from ftf.contract import Dataset


def test_help_lists_the_subcommand(capsys):
    with pytest.raises(SystemExit):
        main(["enrich-domain", "--help"])
    out = capsys.readouterr().out
    assert "--offline" in out and "--from-raw" in out


def test_no_domains_is_a_usage_error(capsys):
    assert main(["enrich-domain"]) == 2
    assert "no domains given" in capsys.readouterr().err


def test_offline_run_writes_unknown_dataset_and_summary(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    summary = tmp_path / "summary.json"
    code = main([
        "enrich-domain", "example.com", "not a domain", "example.com",
        "--offline", "--cache", str(tmp_path / "missing-cache.json"),
        "--tld-cache", str(tmp_path), "--out", str(out), "--summary", str(summary),
    ])
    assert code == 0
    assert capsys.readouterr().out == ""  # the summary went to the file
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["domains_enriched"] == 1
    assert report["invalid_inputs"] == [{"input": "not a domain", "error": "not a registrable domain: 'not a domain'"}]
    assert report["sources"]["rdap"]["unavailable"] == 1
    assert report["indicators"]["registrar"] == 1 and report["indicators"]["cert_pl_listed"] == 1
    dataset = Dataset.from_json(out.read_text(encoding="utf-8"))
    assert [i.source_ref for i in dataset.incidents] == ["example.com"]


def test_from_raw_scope_feeds_the_run(tmp_path, capsys):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "scam_links.txt").write_text("https://scam-target.top/a\n", encoding="utf-8")
    code = main(["enrich-domain", "--from-raw", str(raw), "--offline", "--cache", str(tmp_path / "c.json"), "--tld-cache", str(tmp_path)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"]["domains"] == 1
    assert [d["domain"] for d in report["domain_details"]] == ["scam-target.top"]


def test_duplicate_registrable_domain_is_reported_not_enriched_twice(tmp_path, capsys):
    summary = tmp_path / "s.json"
    code = main(["enrich-domain", "https://example.com/a", "example.com", "--offline", "--cache", str(tmp_path / "c.json"),
                 "--tld-cache", str(tmp_path), "--summary", str(summary)])
    assert code == 0
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["domains_enriched"] == 1
    assert report["invalid_inputs"] == [{"input": "example.com", "error": "duplicate of example.com; already enriched"}]


def test_missing_scope_directory_is_a_usage_error(tmp_path, capsys):
    code = main(["enrich-domain", "--from-raw", str(tmp_path / "nope"), "--offline", "--cache", str(tmp_path / "c.json")])
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


def test_duplicate_is_skipped_before_any_lookup(tmp_path, capsys, monkeypatch):
    from ftf.enrich import commands
    from enrich_support import FakeNet, client_for, NOW
    from ftf.enrich.domain import Enricher as RealEnricher

    net = FakeNet()

    def make(cache, **kwargs):
        return RealEnricher(cache, client=client_for(net), clock=lambda: NOW, **kwargs)

    monkeypatch.setattr(commands, "Enricher", make)
    summary = tmp_path / "s.json"
    assert main(["enrich-domain", "example.com", "https://www.example.com/x", "--cache", str(tmp_path / "c.json"),
                 "--tld-cache", str(tmp_path), "--summary", str(summary)]) == 0
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["domains_enriched"] == 1
    assert report["invalid_inputs"][0]["input"] == "https://www.example.com/x"
    assert len([u for u in net.calls if "example.com" in u and "dns.google" in u]) == 3  # one lookup set, not two
