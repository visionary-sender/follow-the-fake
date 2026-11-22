"""Clustering and open-set metrics, always with raw counts. Never one accuracy number.

Partitions are lists of member lists of capture ids. Scoring is restricted to the label
universe (the captures the label partition covers); a labelled capture that the prediction does
not place anywhere counts as a predicted singleton.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from itertools import combinations
from typing import Any

from ftf.contract import Dataset


def _assign(partition: Iterable[Iterable[str]], universe: set[str]) -> dict[str, int]:
    """Item -> cluster index, restricted to `universe`; unplaced items become singletons."""
    out: dict[str, int] = {}
    n = 0
    for members in partition:
        for m in members:
            if m in universe and m not in out:
                out[m] = n
        n += 1
    for m in sorted(universe - out.keys()):
        out[m] = n
        n += 1
    return out


def _pairs(assign: dict[str, int]) -> set[tuple[str, str]]:
    by: dict[int, list[str]] = {}
    for item, c in assign.items():
        by.setdefault(c, []).append(item)
    return {tuple(sorted(p)) for ms in by.values() for p in combinations(sorted(ms), 2)}  # type: ignore[misc]


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def pairwise(predicted: Sequence[Sequence[str]], truth: Sequence[Sequence[str]]) -> dict[str, Any]:
    """Pairwise precision/recall/F1 with counts: tp, predicted_pairs, true_pairs."""
    universe = {m for g in truth for m in g}
    p = _pairs(_assign(predicted, universe))
    t = _pairs(_assign(truth, universe))
    tp = len(p & t)
    prec, rec = _ratio(tp, len(p)), _ratio(tp, len(t))
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {
        "items": len(universe), "tp": tp, "predicted_pairs": len(p), "true_pairs": len(t),
        "precision": prec, "recall": rec, "f1": f1,
        "text": f"P {tp} of {len(p)} pairs, R {tp} of {len(t)} pairs",
    }


def _bcubed_items(predicted: Sequence[Sequence[str]], truth: Sequence[Sequence[str]]) -> dict[str, tuple[float, float]]:
    universe = {m for g in truth for m in g}
    pa, ta = _assign(predicted, universe), _assign(truth, universe)
    psize, tsize = Counter(pa.values()), Counter(ta.values())
    both = Counter((pa[i], ta[i]) for i in universe)
    return {i: (both[(pa[i], ta[i])] / psize[pa[i]], both[(pa[i], ta[i])] / tsize[ta[i]]) for i in universe}


def bcubed(predicted: Sequence[Sequence[str]], truth: Sequence[Sequence[str]]) -> dict[str, Any]:
    """Item-level B-cubed precision/recall (Amigo et al. 2009), averaged over items."""
    per = _bcubed_items(predicted, truth)
    n = len(per)
    prec = sum(p for p, _ in per.values()) / n if n else None
    rec = sum(r for _, r in per.values()) / n if n else None
    return {"items": n, "precision": prec, "recall": rec}


def macro_bcubed(predicted: Sequence[Sequence[str]], truth: Sequence[Sequence[str]]) -> dict[str, Any]:
    """B-cubed averaged first within each label group, then over groups (big groups do not dominate)."""
    per = _bcubed_items(predicted, truth)
    rows = []
    for g in truth:
        ms = [m for m in g if m in per]
        if ms:
            rows.append((sum(per[m][0] for m in ms) / len(ms), sum(per[m][1] for m in ms) / len(ms)))
    k = len(rows)
    return {
        "groups": k,
        "precision": sum(p for p, _ in rows) / k if k else None,
        "recall": sum(r for _, r in rows) / k if k else None,
    }


def must_not_link(predicted: Sequence[Sequence[str]], pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """How many must-not-link pairs the prediction puts in one cluster."""
    where: dict[str, int] = {}
    for n, members in enumerate(predicted):
        for m in members:
            where.setdefault(m, n)
    bad = [(a, b) for a, b in pairs if a in where and where.get(a) == where.get(b)]
    return {"pairs": len(pairs), "violated": len(bad), "violations": [list(p) for p in bad]}


def openset_counts(outcomes: Iterable[tuple[set[str] | None, str, set[str] | None]]) -> dict[str, int]:
    """Open-set assignment counts.

    Each outcome is (expected members or None if the item should be new, decision
    'assign'/'new'/'review', members of the assigned campaign or None). An assignment is correct
    when the assigned campaign overlaps the expected group. A false accept is an `assign` for an
    item that should have been new.
    """
    c = Counter({"items": 0, "correct": 0, "wrong": 0, "new": 0, "new_correct": 0, "review": 0, "false_accepts": 0})
    for expected, decision, assigned in outcomes:
        c["items"] += 1
        if decision == "assign":
            if expected is None:
                c["false_accepts"] += 1
            elif assigned and expected & assigned:
                c["correct"] += 1
            else:
                c["wrong"] += 1
        elif decision == "new":
            c["new"] += 1
            c["new_correct"] += expected is None
        else:
            c["review"] += 1
    return dict(c)


def predicted_partition(ds: Dataset, relation: str) -> list[list[str]]:
    """Campaign members of one relation, one entry per canonical capture.

    A duplicate is scored as its canonical capture, so it is dropped from every cluster: the
    canonical capture keeps its own cluster and is counted once, even when a duplicate was linked
    elsewhere. Clusters left empty by this are dropped.
    """
    dups = {c.id for inc in ds.incidents for c in inc.captures if c.duplicate_of}
    out = [sorted({m for m in c.capture_ids if m not in dups}) for c in ds.campaigns if c.relation.value == relation]
    return [members for members in out if members]


def score_against_labels(clustered: Dataset, labels: dict[str, Any]) -> dict[str, Any]:
    """Every label level against the engine's partition of the level's relation."""
    levels = []
    by_level: dict[str, list[dict[str, Any]]] = {}
    for g in labels["groups"]:
        by_level.setdefault(g["level"], []).append(g)
    for level, groups in sorted(by_level.items()):
        relation = groups[0]["relation"]
        truth = [g["members"] for g in groups]
        pred = predicted_partition(clustered, relation)
        universe = {m for t in truth for m in t}
        mnl = [(p["a"], p["b"]) for p in labels["must_not_link"] if p["relation"] == relation]
        levels.append({
            "level": level,
            "relation": relation,
            "source": groups[0]["source"],
            "label_groups": len(truth),
            "label_groups_non_singleton": sum(1 for t in truth if len(t) > 1),
            "pairwise": pairwise(pred, truth),
            "bcubed": bcubed(pred, truth),
            "macro": macro_bcubed(pred, truth),
            "predicted_clusters_touching": len({i for i, c in enumerate(pred) if universe & set(c)}),
            "must_not_link": must_not_link(pred, mnl),
        })
    return {"dataset": clustered.name, "levels": levels}
