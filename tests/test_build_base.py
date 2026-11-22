"""`ftf build-base` with stub stage modules on a synthetic folder (no organiser data)."""

import json
import os
import sys
import types

import pytest

import ftf.features.pipeline as features_pipeline
from ftf.cli import main
from ftf.contract import Dataset, Incident


def _incident(name: str) -> dict:
    return {
        "id": f"case-{name}",
        "source": "synthetic",
        "captures": [{
            "id": f"case-{name}/lander",
            "stage": "lander",
            "artefacts": [{"id": f"case-{name}/lander/html", "type": "html", "path": f"{name}/page.html"}],
        }],
    }


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    for name in ("a", "b"):
        (root / name).mkdir(parents=True)
        (root / name / "page.html").write_text("<script>alert(1)</script>")
    return root


@pytest.fixture
def no_stages(monkeypatch):
    """Ingestion and linking absent; features absent too unless a test patches `extract` back."""
    monkeypatch.setitem(sys.modules, "ftf.ingest", None)
    monkeypatch.setitem(sys.modules, "ftf.linking", None)
    monkeypatch.delattr(features_pipeline, "extract")


@pytest.fixture
def stub_stages(monkeypatch):
    calls = []

    def ingest(inputs, root, name):
        calls.append(("ingest", os.environ.get("FTF_OFFLINE")))
        assert inputs == [root]
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
        return Dataset.model_validate({"name": "stub", "incidents": [_incident(n) for n in names]})

    def extract(incident: Incident, raw_root, out_dir):
        calls.append(("features", incident.id))
        (out_dir / "vectors").mkdir(exist_ok=True)
        (out_dir / "vectors" / "stub.keys.json").write_text("[]")
        cap = incident.captures[0].id
        ind = {"id": f"{incident.id}/kit", "capture_id": cap, "layer": "kit", "kind": "kit_hash",
               "level": "platform", "value": "k-1", "observed": "static", "extractor": "stub@0"}
        return Incident.model_validate({**incident.model_dump(), "indicators": [ind]})

    def link_dataset(ds: Dataset) -> Dataset:
        calls.append(("link", len(ds.incidents)))
        a, b = ds.incidents
        link = {"id": "L1", "a": a.captures[0].id, "b": b.captures[0].id, "relation": "template",
                "level": "medium", "score_bits": 5.0, "method": "stub",
                "evidence": [{"layer": "kit", "kind": "kit_hash", "status": "shared", "value": "k-1",
                              "bits": 5.0, "indicator_ids": [a.indicators[0].id, b.indicators[0].id]}]}
        return Dataset.model_validate({**ds.model_dump(), "links": [link]})

    monkeypatch.setitem(sys.modules, "ftf.ingest", types.SimpleNamespace(ingest=ingest))
    monkeypatch.setitem(sys.modules, "ftf.linking", types.SimpleNamespace(link_dataset=link_dataset))
    monkeypatch.setattr(features_pipeline, "extract", extract)
    monkeypatch.delenv("FTF_OFFLINE", raising=False)
    return calls


def test_all_stages_in_order(raw, tmp_path, stub_stages, capsys):
    out = tmp_path / "base"
    assert main(["build-base", "--raw", str(raw), "--out", str(out), "--offline"]) == 0
    assert stub_stages == [("ingest", "1"), ("features", "case-a"), ("features", "case-b"), ("link", 2)]
    assert "FTF_OFFLINE" not in os.environ  # restored after the run
    ds = Dataset.from_json((out / "dataset.json").read_bytes())
    assert [i.id for i in ds.incidents] == ["case-a", "case-b"]
    assert [link.id for link in ds.links] == ["L1"]
    assert (out / "vectors" / "stub.keys.json").is_file()
    report = json.loads((out / "build-report.json").read_text())
    assert [s["status"] for s in report["stages"]] == ["ok", "ok", "ok"]
    assert capsys.readouterr().out.strip() == str(out / "dataset.json")


def test_missing_ingestion_is_an_empty_base_and_refused(raw, tmp_path, no_stages, capsys):
    out = tmp_path / "base"
    assert main(["build-base", "--raw", str(raw), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "ingest: skipped" in err and "no linkable indicators" in err
    assert not (out / "dataset.json").exists()


def test_only_collector_indicators_are_refused(raw, tmp_path, stub_stages, monkeypatch, capsys):
    def extract(incident: Incident, raw_root, out_dir):
        ind = {"id": f"{incident.id}/vp", "capture_id": incident.captures[0].id, "layer": "collector",
               "kind": "viewport", "level": "capture", "value": "1280x720", "observed": "static",
               "extractor": "stub@0"}
        return Incident.model_validate({**incident.model_dump(), "indicators": [ind]})

    monkeypatch.setattr(features_pipeline, "extract", extract)
    out = tmp_path / "base"
    assert main(["build-base", "--raw", str(raw), "--out", str(out)]) == 1
    assert "no linkable indicators" in capsys.readouterr().err
    assert not (out / "dataset.json").exists()
    assert ("link", 2) not in stub_stages  # linking never ran


def test_missing_linking_still_writes_base(raw, tmp_path, stub_stages, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ftf.linking", None)
    out = tmp_path / "base"
    assert main(["build-base", "--raw", str(raw), "--out", str(out)]) == 0
    ds = Dataset.from_json((out / "dataset.json").read_bytes())
    assert len(ds.incidents) == 2 and ds.links == []
    assert "link: skipped" in capsys.readouterr().err
    report = json.loads((out / "build-report.json").read_text())
    assert [s["status"] for s in report["stages"]] == ["ok", "ok", "skipped"]


def test_existing_base_needs_force(raw, tmp_path, stub_stages):
    out = tmp_path / "base"
    assert main(["build-base", "--raw", str(raw), "--out", str(out)]) == 0
    assert main(["build-base", "--raw", str(raw), "--out", str(out)]) == 2
    assert main(["build-base", "--raw", str(raw), "--out", str(out), "--force"]) == 0


def test_out_inside_raw_is_refused(raw, no_stages, capsys):
    assert main(["build-base", "--raw", str(raw), "--out", str(raw / "base")]) == 2
    assert "outside --raw" in capsys.readouterr().err
    assert not (raw / "base").exists()


def test_missing_raw_folder(tmp_path, no_stages):
    assert main(["build-base", "--raw", str(tmp_path / "nope"), "--out", str(tmp_path / "base")]) == 2


def test_ingest_with_links_is_refused(raw, tmp_path, stub_stages, monkeypatch):
    def ingest(inputs, root, name):
        return Dataset.model_validate({"name": "x", "incidents": [_incident("a")],
                                       "campaigns": [{"id": "C", "label": "c", "relation": "template",
                                                      "capture_ids": ["case-a/lander"], "link_ids": []}]})

    monkeypatch.setitem(sys.modules, "ftf.ingest", types.SimpleNamespace(ingest=ingest))
    assert main(["build-base", "--raw", str(raw), "--out", str(tmp_path / "base")]) == 2
    assert not (tmp_path / "base" / "dataset.json").exists()
