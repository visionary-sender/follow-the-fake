"""E1 hidden-layer recovery and E7 time split on a small synthetic dataset (invented values only)."""

import json

import pytest

from ftf.cli import main
from ftf.contract import Campaign, Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Provenance, Relation
from ftf.evaluate.commands import EvalInputError
from ftf.evaluate.hidden import (
    folder_names,
    hidden_dataset,
    informative_cases,
    render_markdown as render_hidden,
    run_hidden,
    score_levels,
    unit_map,
    unit_partition,
)
from ftf.evaluate.timesplit import render_markdown as render_split, run_timesplit, split_incidents
from ftf.evaluate.units import SPEC_SCHEMA, GroundTruthSpec

PIX_A = "1000000000000001"
PIX_B = "1000000000000002"
PIX_C = "1000000000000003"
PIX_X = "1000000000000004"

P1, P2 = "BACKUP:p1:c1", "BACKUP:p2:c1"
D1, D2, D3, D4 = "do_udostepnienia:1:c1", "do_udostepnienia:2:c1", "do_udostepnienia:3:c1", "do_udostepnienia:4:c1"

_SEQ = iter(range(10**6))


def _ind(cap, kind, value, layer, level):
    return Indicator(id=f"{cap}/{kind}/{next(_SEQ)}", capture_id=cap, layer=layer, kind=kind, level=level,
                     value=value, observed=Observed.static, provenance=Provenance(), extractor="test@0")


def _platform(cap, campaign):
    return _ind(cap, "aio_campaign", campaign, Layer.platform, Level.campaign)


def _pixel(cap, pixel):
    return _ind(cap, "meta_pixel", pixel, Layer.tracking_account, Level.operator)


def _incident(folder, source, cap, date, campaign, pixel):
    return Incident(
        id=folder, source=source, source_ref=folder.split(":", 1)[1],
        captures=[Capture(id=cap, stage="lander", captured_on=date)],
        indicators=[_platform(cap, campaign), _pixel(cap, pixel)],
    )


def make_dataset() -> Dataset:
    """Reference: two BACKUP folders dated 8 October. Later: four do_udostepnienia folders.

    Campaign truth (platform id): {p1, d1}, {p2, d3}, {d2}, {d4}. Visible pixels: p1 and d1 share
    PIX_A, p2 and d4 share PIX_B (a wrong merge for the baseline), d3 has PIX_X (nothing shared).
    """
    return Dataset(name="synthetic", incidents=[
        _incident("BACKUP:p1", "BACKUP", P1, "2026-10-08", "900001", PIX_A),
        _incident("BACKUP:p2", "BACKUP", P2, "2026-10-08", "900002", PIX_B),
        _incident("do_udostepnienia:1", "do_udostepnienia", D1, "2026-10-09", "900001", PIX_A),
        _incident("do_udostepnienia:2", "do_udostepnienia", D2, None, "900003", PIX_C),
        _incident("do_udostepnienia:3", "do_udostepnienia", D3, "2026-10-10", "900002", PIX_X),
        _incident("do_udostepnienia:4", "do_udostepnienia", D4, "2026-10-11", "900004", PIX_B),
    ])


def make_spec(owner=None, must_not_link=None) -> GroundTruthSpec:
    expected = {"campaign": [[P1, D1], [P2, D3], [D2], [D4]]}
    if owner is not None:
        expected["owner"] = owner
    data = {"schema": SPEC_SCHEMA, "expected_e1": expected}
    if must_not_link is not None:
        data["must_not_link"] = must_not_link
    return GroundTruthSpec.from_dict(data)


def _write(tmp_path, ds, spec_data):
    data = tmp_path / "dataset.json"
    data.write_text(ds.to_json())
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(spec_data))
    return ["--dataset", str(data), "--spec", str(spec), "--out-dir", str(tmp_path / "out")]


# E1

def test_hidden_layer_is_removed_and_leak_free():
    ds = make_dataset()
    hidden = hidden_dataset(ds)
    kinds = {i.kind for i in hidden.indicators()}
    assert "aio_campaign" not in kinds and "meta_pixel" in kinds
    assert len(hidden.indicators()) == len(ds.indicators()) - 6


def test_hidden_dataset_without_platform_ids_fails_loudly():
    ds = Dataset(name="no-ids", incidents=[
        Incident(id="X:1", source="X", source_ref="1", captures=[Capture(id="X:1:c1", stage="lander")],
                 indicators=[_pixel("X:1:c1", PIX_A)]),
    ])
    with pytest.raises(EvalInputError):
        hidden_dataset(ds)


def test_e1_scores_campaign_and_reports_the_wrong_merge():
    result = run_hidden(make_dataset(), make_spec(), "baseline")
    assert result["leak_check_hits"] == 0
    row = next(r for r in result["levels"] if r["level"] == "campaign")
    assert (row["pairwise"]["tp"], row["pairwise"]["predicted_pairs"], row["pairwise"]["true_pairs"]) == (1, 2, 2)
    assert row["bcubed"]["items"] == 6
    wrong = result["wrong_merges"]
    assert [(w["a"], w["b"]) for w in wrong] == [("BACKUP:p2", "do_udostepnienia:4")]
    assert wrong[0]["evidence"] and "value" not in wrong[0]["evidence"][0]
    assert "Leak check: 0 hits" in render_hidden(result)


def test_e1_reports_analyst_must_not_link_violations():
    spec = make_spec(must_not_link=[{"a": [P1], "b": [D1], "relation": "campaign", "reason": "invented"}])
    wrong = run_hidden(make_dataset(), spec, "baseline")["wrong_merges"]
    analyst = [w for w in wrong if w["sources"] == ["analyst_verdict"]]
    assert [(w["a"], w["b"]) for w in analyst] == [("BACKUP:p1", "do_udostepnienia:1")]
    assert all("reason" not in w for w in analyst)


def test_wrong_merge_that_is_also_an_analyst_violation_is_one_entry():
    spec = make_spec(must_not_link=[{"a": [P2], "b": [D4], "relation": "campaign", "reason": "invented"}])
    wrong = run_hidden(make_dataset(), spec, "baseline")["wrong_merges"]
    assert len(wrong) == 1
    assert (wrong[0]["a"], wrong[0]["b"]) == ("BACKUP:p2", "do_udostepnienia:4")
    assert wrong[0]["sources"] == ["platform_ids", "analyst_verdict"]


SPLIT_C1, SPLIT_C2, REF = "do_udostepnienia:2:c1", "do_udostepnienia:2:c2", "BACKUP:p1:c1"
COLLAPSED = "do_udostepnienia:2"


def _split_dataset() -> Dataset:
    """Two captures of one collapsed incident, and one reference capture (invented ids)."""
    return Dataset(name="split", incidents=[
        Incident(id=COLLAPSED, source="do_udostepnienia", source_ref="2",
                 captures=[Capture(id=SPLIT_C1, stage="lander"), Capture(id=SPLIT_C2, stage="lander")]),
        Incident(id="BACKUP:p1", source="BACKUP", source_ref="p1", captures=[Capture(id=REF, stage="lander")]),
    ])


def _clustered(ds: Dataset, clusters: list[list[str]]) -> Dataset:
    """The dataset with the engine's campaign clusters given explicitly (order as given)."""
    return ds.model_copy(update={"campaigns": [
        Campaign(id=f"cmp:{n}", label="invented", relation=Relation.campaign, capture_ids=c)
        for n, c in enumerate(clusters, start=1)
    ]})


def _collapse_spec(**expected) -> GroundTruthSpec:
    return GroundTruthSpec.from_dict({"schema": SPEC_SCHEMA, "collapse_incidents": [COLLAPSED],
                                      "expected_e1": expected})


def test_split_unit_is_counted_and_joins_neither_side():
    ds = _split_dataset()
    spec = _collapse_spec(campaign=[[COLLAPSED, REF]], owner=[[COLLAPSED], [REF]])
    units = unit_map(ds, spec)
    assert units[SPLIT_C1] == units[SPLIT_C2] == COLLAPSED
    # the engine puts the two captures of the collapsed incident in different clusters, in either order
    for clusters in ([[SPLIT_C1, REF], [SPLIT_C2]], [[SPLIT_C2], [SPLIT_C1, REF]]):
        part = unit_partition(_clustered(ds, clusters), "campaign", ds, units)
        assert part.split == [COLLAPSED]
        assert part.clusters == [[REF]]
    parts = {"campaign": part, "operator": unit_partition(_clustered(ds, clusters), "operator", ds, units)}
    cases = informative_cases(parts, spec, units, ("campaign", "owner"), folder_names(ds))
    assert len(cases) == 1
    assert cases[0]["levels"]["campaign"] == {"expected": "together", "engine": "apart", "agree": False}


def test_level_scores_count_the_same_collapsed_units():
    ds = _split_dataset()
    spec = _collapse_spec()
    units = unit_map(ds, spec)
    labels = {"groups": [{"level": "campaign", "relation": "campaign", "members": [SPLIT_C1, SPLIT_C2, REF]}],
              "must_not_link": []}
    parts = {"campaign": unit_partition(_clustered(ds, [[SPLIT_C1, SPLIT_C2, REF]]), "campaign", ds, units)}
    row = score_levels(labels, parts, units, ("campaign",))[0]
    assert row["label_groups"] == 1
    assert row["pairwise"]["items"] == 2  # the collapsed incident and the reference capture, not three captures
    assert (row["pairwise"]["tp"], row["pairwise"]["true_pairs"]) == (1, 1)


def test_e1_informative_case_differs_between_levels():
    spec = make_spec(owner=[[P1, P2]])
    result = run_hidden(make_dataset(), spec, "baseline")
    cases = result["informative_cases"]
    assert len(cases) == 1 and {cases[0]["a"], cases[0]["b"]} == {"BACKUP:p1", "BACKUP:p2"}
    assert cases[0]["levels"]["owner"] == {"expected": "together", "engine": "apart", "agree": False}
    assert cases[0]["levels"]["campaign"]["agree"] is True


# E7

def test_split_uses_capture_dates_and_names_the_fallback():
    ref, later, info = split_incidents(make_dataset())
    assert ref == ["BACKUP:p1", "BACKUP:p2"]
    assert later == ["do_udostepnienia:2", "do_udostepnienia:1", "do_udostepnienia:3", "do_udostepnienia:4"]
    assert info["do_udostepnienia:2"]["split_by"].startswith("organiser set")
    assert info["do_udostepnienia:1"]["split_by"] == "capture date"


def test_timesplit_hidden_counts_and_verdicts():
    result = run_timesplit(make_dataset(), make_spec(), "baseline", hide=True)
    counts = result["counts"]
    assert counts["items"] == 4 and counts["correct"] == 1 and counts["false_accepts"] == 1
    assert counts["wrong"] == 0 and counts["new"] == 2 and counts["new_correct"] == 1
    verdict = {it["item"]: it["verdict"] for it in result["items"]}
    assert verdict == {
        "do_udostepnienia:1": "correct assignment",
        "do_udostepnienia:2": "correct new",
        "do_udostepnienia:3": "missed (new, should assign)",
        "do_udostepnienia:4": "false accept",
    }
    assert {i["item"] for i in result["wrong_or_false"]} == {"do_udostepnienia:4"}
    assert "| do_udostepnienia:4 |" in render_split(result)


def test_timesplit_visible_ids_change_the_decision_for_item_three():
    result = run_timesplit(make_dataset(), make_spec(), "baseline", hide=False)
    verdict = {it["item"]: it["verdict"] for it in result["items"]}
    assert verdict["do_udostepnienia:3"] == "correct assignment"
    assert result["counts"]["correct"] == 2


def test_eval_cli_writes_both_outputs(tmp_path, capsys):
    common = _write(tmp_path, make_dataset(), {"schema": SPEC_SCHEMA,
                                               "expected_e1": {"campaign": [[P1, D1], [P2, D3], [D2], [D4]]}})
    assert main(["eval", "hidden", *common]) == 0
    assert main(["eval", "timesplit", *common]) == 0
    hidden = json.loads((tmp_path / "out" / "hidden" / "hidden-baseline.json").read_text())
    split = json.loads((tmp_path / "out" / "timesplit" / "timesplit-baseline-hidden.json").read_text())
    assert hidden["leak_check_hits"] == 0 and split["counts"]["items"] == 4
    assert "engine baseline, platform ids hidden" in capsys.readouterr().out


def test_timesplit_lists_folders_that_bridge_two_truth_groups():
    spec = GroundTruthSpec.from_dict({
        "schema": SPEC_SCHEMA,
        "expected_e1": {"campaign": [[P1, D1], [P2, D3], [D2], [D4]]},
        "analyst_groups": {"analyst_campaign": [{"relation": "campaign", "members": [P1, D3], "reason": "invented"}]},
    })
    result = run_timesplit(make_dataset(), spec, "baseline", hide=True)
    assert result["bridging_folders"] == [
        {"folder": "BACKUP:p1", "groups": 2},
        {"folder": "do_udostepnienia:3", "groups": 2},
    ]
    assert "BACKUP:p1 (2 groups)" in render_split(result)


def test_fragmentation_counts_truth_groups_whose_later_items_all_came_out_new():
    spec = GroundTruthSpec.from_dict({
        "schema": SPEC_SCHEMA,
        "expected_e1": {"campaign": [[P1, D1], [P2, D3], [D2], [D4]]},
        "analyst_groups": {"analyst_campaign": [{"relation": "campaign", "members": [D2, D3], "reason": "invented"}]},
    })
    result = run_timesplit(make_dataset(), spec, "baseline", hide=True)
    decision = {it["item"]: it["decision"] for it in result["items"]}
    assert decision["do_udostepnienia:2"] == decision["do_udostepnienia:3"] == "new"
    assert result["fragmentation"] == 1
    assert result["fragmented_groups"] == [["do_udostepnienia:2", "do_udostepnienia:3"]]
    assert "Limitation: the base is not grown" in render_split(result)


def test_default_spec_has_no_bridging_and_no_fragmentation():
    result = run_timesplit(make_dataset(), make_spec(), "baseline", hide=True)
    assert result["bridging_folders"] == [] and result["fragmentation"] == 0 and result["fragmented_groups"] == []
