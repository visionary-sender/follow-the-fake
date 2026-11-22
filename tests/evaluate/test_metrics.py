"""Metrics against hand-computed examples."""

from pytest import approx

from ftf.evaluate.metrics import bcubed, macro_bcubed, must_not_link, openset_counts, pairwise

TRUTH = [["a", "b", "c"], ["d", "e"]]
PRED = [["a", "b"], ["c", "d", "e"]]


def test_pairwise_counts():
    # truth pairs ab ac bc de; predicted pairs ab cd ce de; shared ab de
    r = pairwise(PRED, TRUTH)
    assert (r["tp"], r["predicted_pairs"], r["true_pairs"]) == (2, 4, 4)
    assert r["precision"] == approx(0.5) and r["recall"] == approx(0.5) and r["f1"] == approx(0.5)
    assert r["text"] == "P 2 of 4 pairs, R 2 of 4 pairs"


def test_bcubed_item_level():
    # per item (P, R): a,b (1, 2/3); c (1/3, 1/3); d,e (2/3, 1)
    r = bcubed(PRED, TRUTH)
    assert r["items"] == 5
    assert r["precision"] == approx(11 / 15) and r["recall"] == approx(11 / 15)


def test_macro_bcubed_averages_groups():
    # group abc: P (1+1+1/3)/3 = 7/9, R 5/9; group de: P 2/3, R 1
    r = macro_bcubed(PRED, TRUTH)
    assert r["groups"] == 2
    assert r["precision"] == approx(13 / 18) and r["recall"] == approx(7 / 9)


def test_unplaced_and_unlabelled_items():
    # c is not placed by the prediction (singleton); x is outside the label universe (ignored)
    r = pairwise([["a", "b", "x"]], [["a", "b", "c"]])
    assert (r["tp"], r["predicted_pairs"], r["true_pairs"]) == (1, 1, 3)


def test_perfect_and_all_singletons():
    assert pairwise(TRUTH, TRUTH)["f1"] == approx(1.0)
    singles = pairwise([], TRUTH)
    assert singles["predicted_pairs"] == 0 and singles["precision"] is None and singles["recall"] == 0
    assert bcubed([], TRUTH)["precision"] == approx(1.0)


def test_must_not_link():
    r = must_not_link(PRED, [("c", "d"), ("a", "e"), ("a", "zz")])
    assert (r["pairs"], r["violated"]) == (3, 1)
    assert r["violations"] == [["c", "d"]]


def test_openset_counts():
    outcomes = [
        ({"a"}, "assign", {"a", "b"}),  # correct
        ({"a"}, "assign", {"c"}),  # wrong
        (None, "assign", {"c"}),  # false accept
        (None, "new", None),  # correct new
        ({"a"}, "new", None),  # missed
        ({"a"}, "review", None),
    ]
    c = openset_counts(outcomes)
    assert c == {"items": 6, "correct": 1, "wrong": 1, "new": 2, "new_correct": 1, "review": 1, "false_accepts": 1}
