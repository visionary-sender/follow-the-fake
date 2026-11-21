import hashlib
import json

import pytest

from ftf.contract import Dataset, Decision
from ftf.contract.fixture import fixture_dir, load_fixture
from ftf.ingest import ids
from ftf.pipeline import AnalysisInput, analyze, guess_kind
from pipeline_synthetic import synthetic_base

STATUSES = {"ok", "skipped", "not_available", "error"}
needs_fixture = pytest.mark.skipif(not (fixture_dir() / "dataset.json").is_file(), reason="fixture not present")


def _valid(result) -> Dataset:
    ds = result.to_dataset()
    again = Dataset.model_validate_json(ds.to_json())
    assert again == ds
    assert ds.incidents[0].id == result.incident.id
    return ds


def test_base_item_returns_precomputed_assignment_links_and_similar(tmp_path):
    base = synthetic_base()
    res = analyze(AnalysisInput(kind="dataset_item", text="s-2"), base, run_dir=tmp_path)
    assert res.from_base and res.ok
    assert res.assignment == base.assignments[0]
    assert {l.id for l in res.links} == {"L1", "L2"}
    assert [s.incident_id for s in res.similar] == ["s-1", "s-3"]  # by bits, best first
    assert [s.name for s in res.stages] == ["ingest", "content features", "enrichment", "link/assign"]
    ds = _valid(res)
    assert {c.id for c in ds.campaigns} == {"C1", "C2"}
    assert {i.id for i in ds.incidents} == {"s-1", "s-2", "s-3"}


def test_base_item_without_assignment_is_review(tmp_path):
    res = analyze(AnalysisInput(kind="dataset_item", text="s-1"), synthetic_base(), run_dir=tmp_path)
    assert res.assignment.decision is Decision.review
    assert res.assignment.campaign_id is None
    _valid(res)


def test_unknown_base_item_is_an_error_but_still_valid(tmp_path):
    res = analyze(AnalysisInput(kind="dataset_item", text="nope"), synthetic_base(), run_dir=tmp_path)
    assert not res.ok
    assert res.stages[0].status == "error"
    assert res.assignment.decision is Decision.review
    _valid(res)


@pytest.mark.parametrize("kind,name,data", [
    ("screenshot", "ad shot.png", b"\x89PNG\r\n\x1a\nnot really an image"),
    ("har", "page.har", b'{"log": {"entries": []}}'),
    ("html", "page.html", b"<script>alert(1)</script>"),
    ("mhtml", "page.mhtml", b"MIME-Version: 1.0"),
    ("media", "ad.mp4", b"\x00\x00\x00\x18ftypmp42"),
])
def test_file_inputs_are_stored_hashed_and_reviewed(tmp_path, kind, name, data):
    src = tmp_path / "in" / name
    src.parent.mkdir()
    src.write_bytes(data)
    run = tmp_path / "run"
    res = analyze(AnalysisInput(kind=kind, path=src, name=name), synthetic_base(), run_dir=run)
    assert res.ok, res.stages
    assert all(s.status in STATUSES for s in res.stages)
    assert res.incident.id == ids.input_incident_id([hashlib.sha256(data).hexdigest()])
    (art,) = res.incident.captures[0].artefacts
    assert art.sha256 == hashlib.sha256(data).hexdigest()
    assert (res.input_root / art.path).read_bytes() == data and res.input_root == run / "input"
    assert res.assignment.decision is Decision.review
    missing = {m.modality.value for m in res.incident.captures[0].missing}
    assert "ad_library" in missing and art.type.value not in missing
    _valid(res)


def test_keyword_input(tmp_path):
    res = analyze(AnalysisInput(kind="keyword", text="bitcoin celebrity"), synthetic_base(), run_dir=tmp_path)
    assert res.ok
    assert res.assignment.decision is Decision.review and res.assignment.note
    assert res.incident.captures[0].artefacts[0].type.value == "text"
    _valid(res)


def test_url_input_offline_enrichment_from_empty_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no enrichment cache here
    res = analyze(AnalysisInput(kind="url", text="https://scam.example.com/x"), synthetic_base(), run_dir=tmp_path / "run", offline=True)
    assert res.ok, res.stages
    enrich = next(s for s in res.stages if s.name == "enrichment")
    assert enrich.status == "ok" and "offline" in enrich.note
    assert res.incident.indicators and all(i.value == "unknown" for i in res.incident.indicators)
    missing = {m.modality.value: m.reason for m in res.incident.captures[0].missing}
    assert "sandbox" in missing["har"]
    assert not (tmp_path / "data").exists()  # no cache file created as a side effect
    _valid(res)


def test_case_folder_is_hashed_in_place(tmp_path):
    case = tmp_path / "case"
    (case / "sub").mkdir(parents=True)
    (case / "page.html").write_bytes(b"<html></html>")
    (case / "sub" / "shot.png").write_bytes(b"png")
    res = analyze(AnalysisInput(kind="dataset_item", path=case), synthetic_base(), run_dir=tmp_path / "run")
    assert res.ok, res.stages  # ingestion splits the folder into captures (sub-folder = its own capture)
    types = sorted(a.type.value for c in res.incident.captures for a in c.artefacts)
    assert types == ["ad_screenshot", "html"]
    _valid(res)


def test_input_validation():
    with pytest.raises(ValueError):
        AnalysisInput(kind="nope", text="x")
    with pytest.raises(ValueError):
        AnalysisInput(kind="url", text=" ")
    with pytest.raises(ValueError):
        AnalysisInput(kind="har")


def test_guess_kind(tmp_path):
    base = synthetic_base()
    (tmp_path / "a.har").write_text("{}")
    assert guess_kind(str(tmp_path / "a.har")) == "har"
    assert guess_kind(str(tmp_path)) == "dataset_item"
    assert guess_kind("https://x.example") == "url"
    assert guess_kind("s-1", base) == "dataset_item"
    assert guess_kind("free crypto") == "keyword"


def test_summary_is_json(tmp_path):
    res = analyze(AnalysisInput(kind="dataset_item", text="s-2"), synthetic_base(), run_dir=tmp_path)
    data = json.loads(json.dumps(res.summary()))
    assert data["decision"] == "assign" and data["similar"][0]["incident_id"] == "s-1"


@needs_fixture
def test_fixture_items_match_fixture_assignments(tmp_path):
    base = load_fixture()
    for inc in base.incidents:
        res = analyze(AnalysisInput(kind="dataset_item", text=inc.id), base, run_dir=tmp_path / inc.id)
        assert res.ok
        expected = next((a for a in base.assignments if a.incident_id == inc.id), None)
        if expected is not None:
            assert res.assignment == expected
        else:
            assert res.assignment.decision is Decision.review
        _valid(res)


@needs_fixture
def test_fixture_screenshot_and_keyword_inputs(tmp_path):
    base = load_fixture()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    for inp in (AnalysisInput(kind="screenshot", path=shot), AnalysisInput(kind="keyword", text="test")):
        res = analyze(inp, base, run_dir=tmp_path / inp.kind)
        assert res.ok and res.assignment.decision is Decision.review
        _valid(res)


def test_relative_case_folder(tmp_path, monkeypatch):
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "page.html").write_bytes(b"<html></html>")
    monkeypatch.chdir(tmp_path)
    res = analyze(AnalysisInput(kind="dataset_item", path="case"), synthetic_base(), run_dir=tmp_path / "run")
    assert res.ok, res.stages
    assert [a.path for a in res.incident.captures[0].artefacts] == ["case/page.html"]
    _valid(res)


def test_to_dataset_drops_candidates_outside_the_base(tmp_path, monkeypatch):
    import sys
    import types

    from ftf.contract import Assignment, Candidate

    def link_incident(base, incident, relation=None, backend=None, base_dir=None, incident_dir=None):
        a = Assignment(incident_id=incident.id, candidates=[Candidate(campaign_id="new:1", score_bits=11.0),
                                                            Candidate(campaign_id="C1", score_bits=3.0)],
                       decision=Decision.assign, campaign_id="new:1", evidence_capacity_bits=9.0, method="fake@0")
        return types.SimpleNamespace(assignment=a, links=[])

    mod = types.ModuleType("ftf.linking")
    mod.link_incident = link_incident
    monkeypatch.setitem(sys.modules, "ftf.linking", mod)
    res = analyze(AnalysisInput(kind="keyword", text="x"), synthetic_base(), run_dir=tmp_path)
    ds = _valid(res)
    (a,) = ds.assignments
    assert [c.campaign_id for c in a.candidates] == ["C1"]
    assert a.decision is Decision.review and "new:1" in a.note
