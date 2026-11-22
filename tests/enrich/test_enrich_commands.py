import json

import pytest

from ftf.cli import main
from ftf.contract import Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Provenance, Stage


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


def _dataset_file(tmp_path):
    """A contract Dataset with a landing domain, a shared SDK domain and an ad display domain."""
    cap = Capture(id="case:c1", stage=Stage.unknown, artefacts=[])

    def ind(n, kind, value, layer, level):
        return Indicator(id=f"case:c1:i{n}", capture_id="case:c1", layer=layer, kind=kind, level=level, value=value,
                         observed=Observed.live, provenance=Provenance(artefact_id=None, locator="test"), extractor="domains@test")

    inc = Incident(id="case", source="synthetic", captures=[cap], indicators=[
        ind(1, "landing_domain", "scam-target.top", Layer.landing, Level.landing),
        ind(2, "chain_domain", "facebook.com", Layer.infrastructure, Level.campaign),  # SDK host: shared
        ind(3, "ad_display_domain", "brand-shown.example", Layer.creative, Level.campaign),  # not a page domain
    ])
    path = tmp_path / "ds.json"
    path.write_text(Dataset(name="test", incidents=[inc]).to_json(), encoding="utf-8")
    return path


def test_from_dataset_takes_registered_domains_without_shared_ones(tmp_path):
    summary = tmp_path / "s.json"
    code = main(["enrich-domain", "--from-dataset", str(_dataset_file(tmp_path)), "--offline",
                 "--cache", str(tmp_path / "c.json"), "--tld-cache", str(tmp_path), "--summary", str(summary)])
    assert code == 0
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert [d["domain"] for d in report["domain_details"]] == ["scam-target.top"]
    (scope,) = report["scopes"]
    assert (scope["domains"], scope["shared_dropped"]) == (1, {"count": 1, "by_category": {"sdk": 1}})


def test_from_raw_ingests_the_folder_and_takes_its_domains(tmp_path, capsys):
    case = tmp_path / "raw" / "case1"
    case.mkdir(parents=True)
    page = "https://scam-target.top/a"
    har = {"log": {"pages": [{"id": "p0", "title": page}], "entries": [
        {"pageref": "p0", "_resourceType": "document", "request": {"method": "GET", "url": page},
         "response": {"status": 200, "headers": [], "content": {"mimeType": "text/html", "text": "<html></html>"}}}]}}
    (case / "page.har").write_text(json.dumps(har), encoding="utf-8")
    code = main(["enrich-domain", "--from-raw", str(tmp_path / "raw"), "--offline", "--cache", str(tmp_path / "c.json"),
                 "--tld-cache", str(tmp_path)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert [d["domain"] for d in report["domain_details"]] == ["scam-target.top"]


def test_unreadable_dataset_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not a dataset", encoding="utf-8")
    code = main(["enrich-domain", "--from-dataset", str(bad), "--offline", "--cache", str(tmp_path / "c.json")])
    assert code == 2
    assert "not a readable contract Dataset" in capsys.readouterr().err


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
