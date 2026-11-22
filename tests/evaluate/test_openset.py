import json

import pytest
from evaluate_support import GROUPS, FakeEngine, dataset

from ftf.evaluate.openset import (
    FULL,
    campaign_group,
    run_openset,
    summarise,
    threshold_sweep,
    to_jsonable,
)


def _by(results, protocol, mode=FULL):
    return {(r.held_out, r.incident_id): r for r in results if r.protocol == protocol and r.mode == mode}


@pytest.fixture(scope="module")
def results():
    return run_openset(FakeEngine(), dataset(), GROUPS)


def test_loo_item_full(results):
    loo = _by(results, "loo_item")
    # c1 is alone in C; d1/d2 are exact duplicates, so holding one out holds out both.
    assert {k[1] for k in loo} == {"ia1", "ia2", "ia3", "ib1", "ib2", "imix"}
    assert all(r.outcome == "correct" for r in loo.values())
    mix = loo[("imix", "imix")]
    assert mix.true_groups == ["A", "B"] and mix.known_groups == ["A", "B"]
    assert mix.campaign_groups == ["A"]


def test_loco_full(results):
    loco = _by(results, "loco")
    for g, inc in [("A", "ia1"), ("A", "ia2"), ("A", "ia3"), ("B", "ib1"), ("C", "ic1"), ("D", "id1"), ("D", "id2")]:
        r = loco[(g, inc)]
        assert r.outcome == "new" and r.known_groups == [], (g, inc)
    # Folder-2 style incident: removing A also removes mix/b, which still belongs to B.
    mix = loco[("A", "imix")]
    assert mix.known_groups == ["B"] and mix.outcome == "correct"


def test_degraded_modes(results):
    outcome = lambda mode: {r.outcome for r in results if r.protocol == "loo_item" and r.mode == mode}
    assert outcome("screenshot") == {"new"}  # a face alone never assigns
    assert outcome("ad_text") == {"new"}
    assert outcome("har") == {"correct"}  # the pixel is enough
    assert outcome("html") == {"correct"}
    assert outcome("url") == {"correct"}
    caps = {r.evidence_capacity_bits for r in results if r.mode == "screenshot"}
    assert caps == {0.0}


def test_summary_uses_raw_counts(results):
    rows = {(r["protocol"], r["mode"]): r for r in summarise(results)}
    full = rows[("loo_item", FULL)]
    assert full["items"] == 6 and full["assigned"] == "6 of 6" and full["false_accepts"] == "0 of 6"
    assert full["median_evidence_capacity_bits"] == 30.0
    loco = rows[("loco", FULL)]
    # A: ia1-3 + imix, B: ib1-2 + imix, C: ic1, D: id1-2. imix comes out "correct" twice.
    assert loco["items"] == 10 and loco["new"] == 8 and loco["false_accepts"] == "0 of 10"
    assert len(rows) == 2 * 6  # two protocols x (full + five modes)
    json.dumps(summarise(results))
    json.dumps(to_jsonable(results))


def test_threshold_sweep(results):
    rows = threshold_sweep(results)
    assert rows, "the fake engine exposes candidate scores"
    rates = [r["correct_rate"] for r in rows]
    assert rates == sorted(rates, reverse=True)  # raising the threshold never adds accepts
    assert rows[-1]["correct"] == "0 of 6"
    assert all(r["false_accepts_unknown"].startswith("0 of") for r in rows)
    assert rows[-1]["threshold_bits"] is None
    json.dumps(rows, allow_nan=False)


def test_false_accepts_are_counted():
    # An engine that links on the shared face assigns everything; LOCO members become wrong.
    res = run_openset(FakeEngine(link_faces=True), dataset(), GROUPS, modes=[FULL, "screenshot"])
    rows = {(r["protocol"], r["mode"]): r for r in summarise(res)}
    assert rows[("loco", FULL)]["wrong"] > 0
    assert rows[("loco", "screenshot")]["false_accept_rate"] > 0


def test_campaign_group_plurality_and_tie():
    g = {"a": {"A"}, "b": {"B"}, "c": {"A"}}
    assert campaign_group(["a", "b", "c", "x"], g) == ["A"]
    assert campaign_group(["a", "b"], g) == ["A", "B"]
    assert campaign_group(["x"], g) == []


def test_bad_inputs():
    with pytest.raises(KeyError):
        run_openset(FakeEngine(), dataset(), {"Z": ["nope"]})
    with pytest.raises(ValueError):
        run_openset(FakeEngine(), dataset(), GROUPS, modes=["video"])
    with pytest.raises(ValueError):
        run_openset(FakeEngine(), dataset(), GROUPS, protocols=["lolo"])
