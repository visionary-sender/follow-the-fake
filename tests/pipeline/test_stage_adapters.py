"""Stage adapters against injected fakes with the published signatures (HACK-41)."""

import sys
import types

from ftf.contract import Assignment, Capture, Dataset, Decision, Incident, Indicator, Layer, Level, Observed, Stage
from ftf.pipeline import AnalysisInput, analyze
from pipeline_synthetic import synthetic_base


def _module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_ingest_features_link_adapters(tmp_path, monkeypatch):
    calls = {}

    def ingest(inputs, *, root=None, name="ingest", indicators=True):
        calls["ingest"] = (list(inputs), root)
        inc = Incident(id="input:abcdefabcdef", source="submitted", captures=[Capture(id="input:abcdefabcdef:c1", stage=Stage.lander)])
        return Dataset(name=name, incidents=[inc])

    def extract(incident, raw_root, out_dir):
        calls["features"] = (raw_root, out_dir)
        ind = Indicator(id="input:abcdefabcdef:c1:t", capture_id="input:abcdefabcdef:c1", layer=Layer.creative, kind="page_title",
                        level=Level.campaign, value="Title", observed=Observed.static, extractor="fake@0")
        return incident.model_copy(update={"indicators": [ind]})

    def link_incident(base, incident, relation=None, backend=None, base_dir=None, incident_dir=None):
        calls["link"] = (base.name, incident.id)
        a = Assignment(incident_id=incident.id, decision=Decision.new, evidence_capacity_bits=5.0, method="fake@0")
        return types.SimpleNamespace(assignment=a, links=[])

    _module(monkeypatch, "ftf.ingest", ingest=ingest)
    _module(monkeypatch, "ftf.features", pipeline=None)
    _module(monkeypatch, "ftf.features.pipeline", extract=extract)
    _module(monkeypatch, "ftf.linking", link_incident=link_incident)
    src = tmp_path / "page.html"
    src.write_bytes(b"<html></html>")
    res = analyze(AnalysisInput(kind="html", path=src), synthetic_base(), run_dir=tmp_path / "run")
    assert [s.status for s in res.stages] == ["ok", "ok", "skipped", "ok"], res.stages
    root = tmp_path / "run" / "input"
    assert calls["ingest"] == ([root / "page.html"], root)
    assert calls["features"] == (root, tmp_path / "run" / "features")
    assert calls["link"] == ("synthetic base", "input:abcdefabcdef")
    assert res.assignment.decision is Decision.new and len(res.incident.indicators) == 1
    res.to_dataset()


def test_stage_exception_is_reported_not_raised(tmp_path, monkeypatch):
    def link_incident(base, incident, relation=None, backend=None, base_dir=None, incident_dir=None):
        raise RuntimeError("boom")

    _module(monkeypatch, "ftf.linking", link_incident=link_incident)
    res = analyze(AnalysisInput(kind="keyword", text="x"), synthetic_base(), run_dir=tmp_path)
    assert res.stages[-1].status == "error" and "boom" in res.stages[-1].note
    assert res.assignment.decision is Decision.review
    res.to_dataset()


def test_url_is_never_fetched_without_fetch_flag(tmp_path, monkeypatch):
    def fetch_url(*a, **k):
        raise AssertionError("page fetched")

    _module(monkeypatch, "ftf.fetch", fetch_url=fetch_url)
    monkeypatch.chdir(tmp_path)
    for offline in (True, False):
        # online enrichment alone must not open the page (enrichment itself is stubbed out here)
        monkeypatch.setattr("ftf.pipeline.stages.enrichment", lambda ctx: ("skipped", "stub"))
        monkeypatch.setattr("ftf.pipeline.stages.STAGES", [("ingest", __import__("ftf.pipeline.stages", fromlist=["ingest"]).ingest)])
        res = analyze(AnalysisInput(kind="url", text="https://x.example.com"), synthetic_base(), run_dir=tmp_path / f"run{offline}", offline=offline)
        assert res.stages[0].status == "skipped" and "sandbox" in res.stages[0].note


def test_fetch_flag_refangs_and_ingests_fetch_dir(tmp_path, monkeypatch):
    seen = {}

    def fetch_url(url, out_dir):
        seen["url"] = url
        out_dir.mkdir(parents=True)

    def ingest(inputs, *, root=None, name="ingest", indicators=True):
        seen["ingest"] = (list(inputs), root)
        return Dataset(name=name, incidents=[Incident(id="fetch:x", source="fetch", captures=[Capture(id="fetch:x:c1", stage=Stage.lander)])])

    _module(monkeypatch, "ftf.fetch", fetch_url=fetch_url)
    _module(monkeypatch, "ftf.ingest", ingest=ingest)
    from ftf.pipeline import stages
    ctx = stages.RunContext(inp=AnalysisInput(kind="url", text="hxxps://a[.]example[.]com/p"), base=synthetic_base(),
                            offline=False, run_dir=tmp_path, fetch=True)
    assert stages.ingest(ctx)[0] == "ok"
    assert seen["url"] == "https://a.example.com/p"
    assert seen["ingest"] == ([tmp_path / "fetch"], tmp_path / "fetch") and ctx.input_root == tmp_path / "fetch"


def test_broken_dependency_inside_installed_package_is_an_error(tmp_path, monkeypatch):
    import importlib

    real = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "ftf.ingest":
            raise ModuleNotFoundError("No module named 'somedep'", name="somedep")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    src = tmp_path / "p.html"
    src.write_bytes(b"x")
    res = analyze(AnalysisInput(kind="html", path=src), synthetic_base(), run_dir=tmp_path / "run")
    assert res.stages[0].status == "error" and "somedep" in res.stages[0].note
    res.to_dataset()
