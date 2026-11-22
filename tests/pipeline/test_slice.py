"""End-to-end slice of `ftf analyze`: real ingestion, a stubbed or real linking call, base collisions.

All values are invented (`.example` domains, made-up ids); no organiser data.
"""

import json
import sys
import types
from pathlib import Path

import pytest

from ftf.contract import Assignment, Candidate, Dataset, Decision
from ftf.pipeline import AnalysisInput, analyze
from ftf.pipeline import stages as S
from pipeline_synthetic import synthetic_base

try:
    from ftf.linking import link_dataset, link_incident  # noqa: F401
    HAVE_LINKING = True
except ImportError:
    HAVE_LINKING = False
needs_linking = pytest.mark.skipif(not HAVE_LINKING, reason="ftf.linking.link_incident not importable")

PIXEL = "990011223344556"


def _page(pixel: str | None, title: str) -> bytes:
    script = f"<script>fbq('init', '{pixel}'); fbq('track', 'PageView');</script>" if pixel else ""
    return f"<html><head><title>{title}</title>{script}</head><body><form action='/lead'><input name='phone'></form></body></html>".encode()


def _stub_linking(monkeypatch, calls):
    def link_incident(base, incident, relation=None, backend=None, base_dir=None, incident_dir=None):
        calls.append({"base": base, "incident": incident, "base_dir": base_dir, "incident_dir": incident_dir})
        a = Assignment(incident_id=incident.id, candidates=[Candidate(campaign_id="C1", score_bits=12.0)],
                       decision=Decision.assign, campaign_id="C1", evidence_capacity_bits=20.0, method="stub@0")
        return types.SimpleNamespace(assignment=a, links=[])

    mod = types.ModuleType("ftf.linking")
    mod.link_incident = link_incident
    monkeypatch.setitem(sys.modules, "ftf.linking", mod)


def test_upload_is_ingested_and_linked_with_dirs(tmp_path, monkeypatch):
    calls = []
    _stub_linking(monkeypatch, calls)
    src = tmp_path / "page.html"
    src.write_bytes(_page(PIXEL, "offer"))
    run = tmp_path / "run"
    res = analyze(AnalysisInput(kind="html", path=src, name="page.html"), synthetic_base(), run_dir=run, base_dir=tmp_path / "base")
    by = {s.name: s for s in res.stages}
    assert by["ingest"].status == "ok" and "ftf.ingest" in by["ingest"].note
    assert by["link/assign"].status == "ok"
    assert res.assignment.decision is Decision.assign and res.assignment.campaign_id == "C1"
    (call,) = calls
    assert call["base_dir"] == tmp_path / "base" and call["incident_dir"] == run / "features"
    assert call["incident"].id.startswith("input:")


def test_missing_linking_means_review(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_optional", lambda module, name: None if module == "ftf.linking" else S.importlib.import_module(module).__dict__.get(name))
    res = analyze(AnalysisInput(kind="keyword", text="x"), synthetic_base(), run_dir=tmp_path)
    link = next(s for s in res.stages if s.name == "link/assign")
    assert link.status == "not_available" and res.assignment.decision is Decision.review


def test_case_folder_already_in_base_uses_precomputed(tmp_path, monkeypatch):
    calls = []
    _stub_linking(monkeypatch, calls)
    raw = tmp_path / "raw"
    case = raw / "do_udostępnienia" / "7"
    case.mkdir(parents=True)
    (case / "page.html").write_bytes(_page(PIXEL, "offer"))
    monkeypatch.setenv("FTF_RAW_ROOT", str(raw))
    text = synthetic_base().to_json().replace('"s-1"', '"do_udostepnienia:7"').replace('"s-1/', '"do_udostepnienia:7/')
    base = Dataset.model_validate({**json.loads(text), "links": [], "campaigns": [], "assignments": []})
    res = analyze(AnalysisInput(kind="dataset_item", path=case), base, run_dir=tmp_path / "run")
    assert res.from_base and res.incident.id == "do_udostepnienia:7"
    assert "already holds" in res.stages[0].note
    assert not calls  # never linked against itself


def test_har_page_url_is_enriched_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no enrichment cache here
    har = {"log": {"pages": [{"title": "https://lander.shop.example.com/p?x=1"}], "entries": []}}
    src = tmp_path / "visit.har"
    src.write_text(json.dumps(har))
    res = analyze(AnalysisInput(kind="har", path=src, name="visit.har"), synthetic_base(), run_dir=tmp_path / "run", offline=True)
    enrich = next(s for s in res.stages if s.name == "enrichment")
    assert enrich.status == "ok" and "offline" in enrich.note, res.stages
    assert any(":enrich:" in i.id for i in res.incident.indicators)
    assert not (tmp_path / "data").exists()


def _chain_har(tmp_path) -> Path:
    """Platform hop (stop-listed) -> tracker hop -> landing; the synthetic domains are `.example`."""
    def entry(url, status=200, location=None):
        resp = {"status": status, "headers": [], "content": {"mimeType": "text/html", "text": ""}}
        if location:
            resp["headers"] = [{"name": "Location", "value": location}]
        return {"pageref": "p0", "request": {"method": "GET", "url": url}, "response": resp, "_resourceType": "document"}

    har = {"log": {
        "pages": [{"id": "p0", "title": "https://l.facebook.com/l.php?u=x"}],
        "entries": [
            entry("https://l.facebook.com/l.php?u=x", 302, "https://trk.example.net/click?clickid=c0ffee"),
            entry("https://trk.example.net/click?clickid=c0ffee", 302, "https://shop.example.com/p"),
            entry("https://shop.example.com/p"),
        ],
    }}
    src = tmp_path / "chain.har"
    src.write_text(json.dumps(har))
    return src


def _record_lookups(monkeypatch) -> list[str]:
    from ftf.enrich import Enricher

    looked_up: list[str] = []
    enrich = Enricher.enrich

    def spy(self, name, *args, **kwargs):
        looked_up.append(name)
        return enrich(self, name, *args, **kwargs)

    monkeypatch.setattr(Enricher, "enrich", spy)
    return looked_up


def test_enrichment_looks_up_own_domains_not_shared_hops(tmp_path, monkeypatch):
    """Landing and tracker hop are enriched; the stop-listed platform hop that starts the chain is not."""
    monkeypatch.chdir(tmp_path)  # no enrichment cache here
    looked_up = _record_lookups(monkeypatch)
    src = _chain_har(tmp_path)
    res = analyze(AnalysisInput(kind="har", path=src, name="chain.har"), synthetic_base(), run_dir=tmp_path / "run", offline=True)
    assert next(s for s in res.stages if s.name == "enrichment").status == "ok", res.stages
    assert sorted(set(looked_up)) == ["example.com", "example.net"]


def test_enrichment_of_har_survives_failed_ingestion(tmp_path, monkeypatch):
    """Fallback incident (ingestion returned nothing): own domains are still found and looked up."""
    monkeypatch.chdir(tmp_path)  # no enrichment cache here
    looked_up = _record_lookups(monkeypatch)
    real_optional = S._optional
    empty = lambda *args, **kwargs: Dataset(name="empty")  # noqa: E731
    monkeypatch.setattr(S, "_optional", lambda module, name: empty if (module, name) == ("ftf.ingest", "ingest") else real_optional(module, name))
    src = _chain_har(tmp_path)
    res = analyze(AnalysisInput(kind="har", path=src, name="chain.har"), synthetic_base(), run_dir=tmp_path / "run", offline=True)
    assert next(s for s in res.stages if s.name == "ingest").status == "error", res.stages
    assert sorted(set(looked_up)) == ["example.com", "example.net"]


def test_fallback_id_already_in_base_is_not_linked_again(tmp_path, monkeypatch):
    calls = []
    _stub_linking(monkeypatch, calls)
    url = "https://lander.shop.example/x"
    inc_id = S.adhoc_ids(url)[0]
    text = synthetic_base().to_json().replace('"s-1"', f'"{inc_id}"').replace('"s-1/', f'"{inc_id}/')
    base = Dataset.model_validate(json.loads(text))
    res = analyze(AnalysisInput(kind="url", text=url), base, run_dir=tmp_path)
    assert res.from_base and res.incident.id == inc_id and not calls
    assert [i.id for i in res.to_dataset().incidents].count(inc_id) == 1


def test_fallback_artefact_ids_stay_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_optional", lambda module, name: None)
    case = tmp_path / "case"
    case.mkdir()
    (case / "a b.png").write_bytes(b"one")
    (case / "a-b.png").write_bytes(b"two")
    res = analyze(AnalysisInput(kind="dataset_item", path=case), synthetic_base(), run_dir=tmp_path / "run")
    arts = [a.id for c in res.incident.captures for a in c.artefacts]
    assert len(arts) == 2 and len(set(arts)) == 2
    res.to_dataset()


def test_har_file_url_is_not_enriched(tmp_path):
    har = {"log": {"pages": [{"title": "file:///home/someone/page.html"}], "entries": []}}
    src = tmp_path / "local.har"
    src.write_text(json.dumps(har))
    res = analyze(AnalysisInput(kind="har", path=src, name="local.har"), synthetic_base(), run_dir=tmp_path / "run")
    assert next(s for s in res.stages if s.name == "enrichment").status == "skipped"


# -- real linking (skips until ftf.linking.link_incident is importable) ------------------------


def _real_base(tmp_path) -> Dataset:
    from ftf.ingest import ingest

    root = tmp_path / "corpus"
    for n in range(1, 4):
        d = root / f"case{n}"
        d.mkdir(parents=True)
        (d / "page.html").write_bytes(_page(PIXEL, f"offer {n}"))
    for n in range(4, 7):
        d = root / f"case{n}"
        d.mkdir(parents=True)
        (d / "page.html").write_bytes(_page(f"77{n}0000000000{n}", f"other {n}"))
    ds = ingest([root / f"case{n}" for n in range(1, 7)], root=root, name="synthetic corpus")
    return link_dataset(ds)


@needs_linking
def test_real_linking_variant_unrelated_and_screenshot(tmp_path):
    base = _real_base(tmp_path)
    assert base.campaigns
    variant = tmp_path / "variant.html"
    variant.write_bytes(_page(PIXEL, "offer 9"))
    res = analyze(AnalysisInput(kind="html", path=variant, name="variant.html"), base, run_dir=tmp_path / "r1", base_dir=tmp_path)
    assert res.ok, res.stages
    assert next(s for s in res.stages if s.name == "link/assign").status == "ok"
    assert res.assignment.incident_id == res.incident.id
    res.to_dataset()  # valid contract output whatever the decision

    unrelated = tmp_path / "unrelated.html"
    unrelated.write_bytes(b"<html><head><title>a recipe blog</title></head><body>soup</body></html>")
    res = analyze(AnalysisInput(kind="html", path=unrelated, name="unrelated.html"), base, run_dir=tmp_path / "r2", base_dir=tmp_path)
    assert res.ok, res.stages
    assert res.assignment.decision is not Decision.assign

    shot = tmp_path / "ad.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    res = analyze(AnalysisInput(kind="screenshot", path=shot, name="ad.png"), base, run_dir=tmp_path / "r3", base_dir=tmp_path)
    assert res.ok, res.stages
    assert res.assignment.decision is Decision.review  # a screenshot alone cannot prove a new campaign
