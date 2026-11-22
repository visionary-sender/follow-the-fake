"""E6 sensitivity and the evaluation report on small synthetic data (invented values only)."""

from ftf.contract import Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.evaluate.report import (
    REQUIRED_STEPS,
    _eval_parser,
    build_report,
    limitations,
    registered_eval_commands,
    render_markdown,
)
from ftf.evaluate.sensitivity import compare, incident_pairs, move, run_sensitivity, weight_keys
from ftf.linking.weights import HAND_TABLE

CANARY = "canary-key-7"
PIXEL = "1000000000000001"
_SEQ = iter(range(10**6))


def _ind(cap: str, kind: str, value: str, layer: Layer, level: Level) -> Indicator:
    return Indicator(id=f"{cap}/{kind}/{next(_SEQ)}", capture_id=cap, layer=layer, kind=kind, level=level,
                     value=value, observed=Observed.static, provenance=Provenance(), extractor="test@0")


def _incident(folder: str, date: str, pixel: str, extra: list[Indicator] = ()) -> Incident:
    source, case = folder.split(":", 1)
    cap = f"{folder}:c1"
    return Incident(
        id=folder, source=source, source_ref=case,
        captures=[Capture(id=cap, stage="lander", captured_on=date)],
        indicators=[_ind(cap, "meta_pixel", pixel, Layer.tracking_account, Level.operator), *extra],
    )


def _tiny_dataset() -> Dataset:
    return Dataset(name="synthetic", incidents=[
        _incident("SETA:1", "2026-10-08", PIXEL, [_ind("SETA:1:c1", "server_ip", "192.0.2.1", Layer.infrastructure, Level.operator)]),
        _incident("SETA:2", "2026-10-08", "1000000000000002"),
        _incident("SETB:3", "2026-10-09", PIXEL),
    ])


def test_move_shifts_a_kind_and_a_layer_default_and_floors_at_zero():
    bits = HAND_TABLE.kinds["meta_pixel"].bits
    assert move(HAND_TABLE, "kind:meta_pixel", -2).kinds["meta_pixel"].bits == max(bits - 2, 0.0)
    assert move(HAND_TABLE, "kind:meta_pixel", 1).kinds["meta_pixel"].bits == bits + 1
    assert move(HAND_TABLE, "kind:meta_pixel", -100).kinds["meta_pixel"].bits == 0.0
    default = HAND_TABLE.layer_defaults[Layer.tracking_account]
    assert move(HAND_TABLE, "layer_default:tracking_account", 2).layer_defaults[Layer.tracking_account] == default + 2
    # the rest of the table is untouched
    assert move(HAND_TABLE, "kind:meta_pixel", 1).kinds["server_ip"] == HAND_TABLE.kinds["server_ip"]


def test_weight_keys_lists_used_kinds_and_defaults_for_unlisted_kinds_only():
    ds = Dataset(name="keys", incidents=[
        _incident("SETA:1", "2026-10-08", PIXEL, [
            _ind("SETA:1:c1", "not_in_table", "x", Layer.creative, Level.operator),
        ]),
    ])
    keys = weight_keys(ds)
    assert "kind:meta_pixel" in keys
    assert "layer_default:creative" in keys
    assert "kind:server_ip" not in keys  # in the table, but not used by this dataset


def test_compare_counts_pair_and_verdict_flips():
    base = {"pairs": {("campaign", "a", "b"), ("operator", "c", "d")}, "e7": {"I1": "new", "I2": "assign X"}}
    moved = {"pairs": {("campaign", "a", "b"), ("campaign", "a", "e")}, "e7": {"I1": "review", "I2": "assign X"}}
    out = compare(base, moved)
    assert out["pair_flips"] == {"campaign": 1, "operator": 1}
    assert out["pair_flips_total"] == 2
    assert out["e7_flips"] == 1
    assert out["total"] == 3
    assert compare(base, base)["total"] == 0


def test_run_sensitivity_structure_on_a_tiny_dataset():
    result = run_sensitivity(_tiny_dataset())
    assert result["engine"] == "linking"
    assert result["moves_bits"] == [-2.0, -1.0, 1.0, 2.0]
    assert result["weights_tested"] == len(result["ranking"]) == len(weight_keys(_tiny_dataset()))
    flips = [r["flips_total"] for r in result["ranking"]]
    assert flips == sorted(flips, reverse=True)
    for r in result["ranking"]:
        assert set(r["by_move"]) == {"-2", "-1", "+1", "+2"}
        assert r["flips_total"] == sum(m["total"] for m in r["by_move"].values())
    assert set(result["baseline"]["e7"]) == {"assign", "new", "review"}
    assert result["baseline"]["later_items"] == 1


def test_incident_pairs_maps_captures_to_case_folders_and_hides_unknown_ids():
    records = [{"relation": "campaign", "a": f"SETA:1:{CANARY}", "b": "SETB:3:c1", "state": "link", "bits": 5.0,
                "tau": 4.0, "fragile": True, "flips": [], "worst_case": None}]
    (pair,) = incident_pairs(records, {f"SETA:1:{CANARY}": "SETA:1"})
    assert (pair["a"], pair["b"]) == ("SETA:1", "?")


def _labels() -> dict:
    caps = {
        f"SETA:1:{CANARY}": {"source": "SETA", "folder": "1", "key": CANARY, "canonical": f"SETA:1:{CANARY}"},
        "SETB:3:c1": {"source": "SETB", "folder": "3", "key": "c1", "canonical": "SETB:3:c1"},
        "SETB:4:c1": {"source": "SETB", "folder": "4", "key": "c1", "canonical": "SETB:4:c1"},
    }
    return {
        "captures": caps,
        "groups": [{"id": "g1", "level": "campaign", "relation": "campaign",
                    "members": [f"SETA:1:{CANARY}", "SETB:3:c1"]}],
        "summary": {
            "captures": 3, "canonical_captures": 3,
            "groups_per_level": {"campaign": {"groups": 1, "non_singleton": 1, "captures": 2}},
            "must_not_link_pairs": 1, "missing_captures": [],
        },
    }


def _pair(tp: int, pred: int, true: int) -> dict:
    return {"tp": tp, "predicted_pairs": pred, "true_pairs": true,
            "text": f"P {tp} of {pred} pairs, R {tp} of {true} pairs", "precision": None, "recall": None}


def _hidden(tp: int, pred: int, violated: int) -> dict:
    return {"levels": [{
        "level": "campaign", "relation": "campaign", "label_groups": 1, "label_groups_non_singleton": 1,
        "pairwise": _pair(tp, pred, 1), "macro": {"groups": 1, "precision": 1.0, "recall": 1.0},
        "must_not_link": {"pairs": 1, "violated": violated, "violations": []},
    }]}


def _results() -> dict:
    timesplit = {
        "reference": [{"folder": "SETA:1"}], "later": 1, "fallback_incidents": [],
        "counts": {"items": 1, "correct": 1, "wrong": 0, "false_accepts": 0, "new": 0, "new_correct": 0, "review": 0},
        "items": [{"item": "SETB:3", "date": "2026-10-09", "decision": "assign", "expected": "assign SETA:1",
                   "verdict": "correct assignment"}],
        "bridging_folders": [], "fragmentation": 0, "truth_groups": 1,
    }
    sensitivity = {
        "weights_in_table": 100, "weights_tested": 1,
        "baseline": {"pair_links": {"campaign": 1}, "e7": {"assign": 1, "new": 0, "review": 0}, "later_items": 1},
        "ranking": [{"weight": "kind:meta_pixel", "bits": 3.0, "flips_total": 2, "pair_flips_by_relation": {"campaign": 2},
                     "by_move": {m: {"total": 1 if m == "-2" else 0} for m in ("-2", "-1", "+1", "+2")}}],
        "fragile": {"delta_bits": 2.0, "links": 1, "fragile": 1, "worst_case_only": 0,
                    "by_relation": {"campaign": {"links": 1, "fragile": 1, "worst_case": 0}},
                    "records": [{"relation": "campaign", "a": f"SETA:1:{CANARY}", "b": "SETB:3:c1", "state": "link",
                                 "bits": 5.0, "tau": 4.0, "fragile": True,
                                 "flips": [{"layer": "tracking_account", "delta": -2.0, "to": "review"}],
                                 "worst_case": None}]},
    }
    return {"labels": _labels(), "leak": {"hidden_indicators": 4, "remaining_indicators": 9, "hits": 0},
            "hidden baseline": _hidden(1, 2, 1), "hidden linking": _hidden(1, 1, 0),
            "timesplit baseline": timesplit, "timesplit linking": timesplit, "sensitivity": sensitivity}


def _steps() -> dict:
    ok = {"status": "ok", "exit": 0, "log": None}
    steps = {name: dict(ok) for name in REQUIRED_STEPS}
    steps.update({"openset": {"status": "skipped", "exit": None, "note": "not registered"},
                  "degrade": {"status": "skipped", "exit": None, "note": "not registered"},
                  "distractors": {"status": "skipped", "exit": None, "note": "not registered"}})
    return steps


def test_report_tables_hold_raw_counts_for_both_engines_and_never_print_capture_keys():
    report = build_report(_results(), _steps(), build="abc1234")
    md = render_markdown(report)
    assert "P 1 of 2 pairs, R 1 of 1 pairs" in md  # baseline
    assert "P 1 of 1 pairs, R 1 of 1 pairs" in md  # linking
    assert "| relation vs level |" in md and "## E7 time split" in md and "## E6 sensitivity" in md
    assert "SETA:1" in md and "| SETB:3 |" in md  # case folders are fine
    assert CANARY not in md  # capture keys are never printed
    assert "abc1234" in md


def test_report_prints_only_the_build_commit_not_the_whole_build_file():
    build_txt = "build: main abc1234\ncommand: ftf ingest /data/raw/SECRET_FOLDER\nincidents 3, captures 3"
    md = render_markdown(build_report(_results(), _steps(), build=build_txt))
    assert "main abc1234" in md
    assert "SECRET_FOLDER" not in md and "command:" not in md


def test_report_limitations_are_five_to_eight_sentences_with_run_numbers():
    report = build_report(_results(), _steps(), build=None)
    sentences = report["limitations"]
    assert 5 <= len(sentences) <= 8
    assert any("true campaign pairs" in s for s in sentences)
    assert limitations(report) == sentences


def test_report_marks_absent_engine_and_optional_commands_as_not_run():
    results = _results()
    results.pop("hidden baseline")
    results.pop("timesplit baseline")
    md = render_markdown(build_report(results, _steps(), build=None))
    assert "not run" in md
    assert "| openset | skipped |" in md


def test_registered_commands_include_every_required_step_and_the_report():
    names = registered_eval_commands(_eval_parser())
    assert {"labels", "leak", "hidden", "timesplit", "sensitivity", "report"} <= names
