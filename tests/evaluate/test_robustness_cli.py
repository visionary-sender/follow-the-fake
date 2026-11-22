"""`ftf eval openset|degrade|distractors` end to end on the synthetic dataset (invented values only).

The synthetic dataset gets the platform-id kind the commands need (`aio_campaign`), so the
ground-truth groups come from the labels exactly as they do on the organiser dataset.
"""

import json

from evaluate_support import dataset

from ftf.cli import main
from ftf.evaluate.openset import FULL
from ftf.evaluate.perturb import MODES

CAMPAIGN_OF = {"a1": "A", "a2": "A", "a3": "A", "mix/a": "A", "b1": "B", "b2": "B", "mix/b": "B", "c1": "C",
               "d1": "D", "d2": "D"}


def _inputs(tmp_path):
    raw = json.loads(dataset().to_json())
    for inc in raw["incidents"]:
        for cap in inc["captures"]:
            inc["indicators"].append({
                "id": f"{cap['id']}:aio", "capture_id": cap["id"], "layer": "owner", "kind": "aio_campaign",
                "level": "campaign", "value": f"camp-{CAMPAIGN_OF[cap['id']]}", "observed": "static",
                "extractor": "test@0",
            })
    ds_path = tmp_path / "dataset-indicators.json"
    ds_path.write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "BUILD.txt").write_text("build: main 1234567abc\ninputs: synthetic\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "schema": "ftf-eval-spec@0.2",
        "must_not_link": [{"a": ["a1"], "b": ["b1"], "relation": "campaign", "reason": "synthetic pair"}],
    }), encoding="utf-8")
    corpus = tmp_path / "corpus" / "dataset.json"
    corpus.parent.mkdir()
    ic1 = json.loads(dataset().incidents[5].model_dump_json())
    ia1 = json.loads(dataset().incidents[0].model_dump_json())
    corpus.write_text(json.dumps({"name": "corpus", "incidents": [ic1, ia1]}), encoding="utf-8")
    (corpus.parent / "corpus_kind.jsonl").write_text(
        '{"incident_id": "ic1", "corpus_kind": "must_not_link"}\n'
        '{"incident_id": "ia1", "corpus_kind": "must_not_link_blocked"}\n',
        encoding="utf-8",
    )
    return ds_path, spec, corpus


def test_openset_writes_raw_counts_and_build(tmp_path):
    ds_path, spec, _ = _inputs(tmp_path)
    out = tmp_path / "out"
    common = ["--dataset", str(ds_path), "--spec", str(spec), "--out-dir", str(out), "--engine", "baseline"]
    assert main(["eval", "openset", *common]) == 0
    doc = json.loads((out / "openset" / "baseline.json").read_text(encoding="utf-8"))
    assert doc["dataset_build"] == "1234567abc"
    assert doc["modes"] == [FULL]
    loo = [r for r in doc["summary"] if r["protocol"] == "loo_item" and r["mode"] == FULL]
    assert loo and sum(r["items"] for r in loo) == 6  # ia1-3, ib1-2, imix (the A and B items)
    for r in doc["summary"]:
        assert {"correct", "wrong", "new", "review", "false_accepts"} <= set(r)
    assert (out / "openset" / "baseline.md").read_text(encoding="utf-8").startswith("# E2")


def test_degrade_runs_every_mode_with_the_full_reference(tmp_path):
    ds_path, spec, _ = _inputs(tmp_path)
    out = tmp_path / "out"
    assert main(["eval", "degrade", "--dataset", str(ds_path), "--spec", str(spec),
                 "--out-dir", str(out), "--engine", "baseline"]) == 0
    doc = json.loads((out / "degrade" / "baseline.json").read_text(encoding="utf-8"))
    assert doc["modes"] == [FULL, *MODES]
    assert {r["mode"] for r in doc["summary"]} == {FULL, *MODES}


def test_distractors_cover_every_code_with_an_expected_answer(tmp_path):
    ds_path, spec, corpus = _inputs(tmp_path)
    out = tmp_path / "out"
    assert main(["eval", "distractors", "--dataset", str(ds_path), "--spec", str(spec), "--corpus", str(corpus),
                 "--out-dir", str(out), "--engine", "baseline"]) == 0
    doc = json.loads((out / "distractors" / "baseline.json").read_text(encoding="utf-8"))
    assert {r["code"] for r in doc["results"]} == {"a", "b", "c", "d", "e", "f"}
    assert all(r["expected"] for r in doc["results"])
    assert doc["selection"]["brand_negatives_tagged"] == 1  # the blocked fetch is not counted
    brand = [r for r in doc["results"] if r["code"] == "e"]
    assert [r["name"] for r in brand] == ["brand_negative:ic1"]
    assert doc["dataset_build"] == "1234567abc"


def test_campaign_groups_merge_identical_member_sets():
    from ftf.evaluate.robustness import campaign_groups

    labels = {"groups": [
        {"id": "campaign:0", "relation": "campaign", "members": ["x", "y"]},
        {"id": "landing:0", "relation": "campaign", "members": ["y", "x"]},
        {"id": "campaign:1", "relation": "campaign", "members": ["z"]},
        {"id": "tenant:0", "relation": "operator", "members": ["x", "z"]},
    ]}
    assert campaign_groups(labels) == {"campaign:0+landing:0": ["x", "y"]}
