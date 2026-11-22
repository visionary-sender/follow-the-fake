"""E1 hidden-layer recovery: link the dataset with the platform-id layer hidden.

`hidden_dataset` removes the layer (`ftf.evaluate.leak.hide_platform_ids`) and refuses to go on
unless `leak_check` finds 0 hits. The engine then links the hidden dataset, and each relation's
partition is scored against the ground-truth groups of the levels it stands for: campaign
(relation campaign), owner and tenant (relation operator).

Scoring unit, one mapping for every table (`unit_map`): a unit is a capture, except that the
captures of an incident listed in the spec's `collapse_incidents` are one unit, named by the
incident id. Exact duplicates are not scored; they count as their canonical capture. The same
mapping is applied to the label groups, the must-not-link pairs, the engine's clusters and the
expected E1 groups, so the level scores, the wrong merges and the informative cases count the same
units. A unit whose captures the engine put in more than one cluster is split: it joins no cluster
(so it is not merged with either side) and it is listed under `scoring_units.split_units`.

Informative cases are pairs that the spec's expected E1 groups put together at one level and
apart at another. The expectations are read from the private spec, never from this module.
Folders are shown as case-folder ids (`do_udostepnienia:61`, `BACKUP:p3#2` for the second capture
of a folder); domains and other organiser values are never printed.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from ftf.contract import Dataset
from ftf.evaluate.engine import get_engine
from ftf.evaluate.labels import PLATFORM_LEVELS, build_labels
from ftf.evaluate.leak import hide_platform_ids, leak_check
from ftf.evaluate.metrics import (
    bcubed,
    macro_bcubed,
    merge_overlapping,
    must_not_link,
    pairwise,
    predicted_partition,
)
from ftf.evaluate.units import GroundTruthSpec, incident_of

E1_LEVELS = ("campaign", "owner", "tenant")
ANALYST_LEVEL = "analyst_must_not_link"
UNIT_DEFINITION = (
    "A capture is one unit; the captures of an incident in collapse_incidents are one unit named by the "
    "incident id. Exact duplicates are not scored (they count as their canonical capture). The same mapping "
    "is applied to label groups, must-not-link pairs, engine clusters and expected E1 groups. A unit whose "
    "captures the engine put in more than one cluster is split: it joins no cluster and is listed in split_units."
)


def dataset_build(dataset_path: str | Path) -> str | None:
    """The build record Ingestion writes next to the dataset (BUILD.txt), read at run time; None if absent."""
    build = Path(dataset_path).parent / "BUILD.txt"
    return build.read_text(encoding="utf-8").strip() if build.is_file() else None


def _capture_ids(ds: Dataset) -> set[str]:
    return {c.id for inc in ds.incidents for c in inc.captures}


def _canonical_ids(ds: Dataset) -> list[str]:
    return sorted(c.id for inc in ds.incidents for c in inc.captures if not c.duplicate_of)


def hidden_dataset(ds: Dataset) -> Dataset:
    """The dataset without the platform-id layer. Raises if any hidden value is still visible."""
    from ftf.evaluate.commands import EvalInputError  # noqa: PLC0415

    hidden, values = hide_platform_ids(ds)
    if not values:
        raise EvalInputError("no platform ids to hide; nothing to recover")
    hits = leak_check(hidden, values)
    if hits:
        first = hits[0]
        raise EvalInputError(
            f"leak check: {len(hits)} hidden-value hits after hiding (first at {first.path}, kind {first.kind}); "
            "refusing to run"
        )
    if _capture_ids(hidden) != _capture_ids(ds):
        raise EvalInputError("hiding changed capture ids: a hidden value is part of an id; refusing to run")
    return hidden


def folder_names(ds: Dataset) -> dict[str, str]:
    """Capture id -> case-folder id (`BACKUP:p3`), with `#k` when the folder has several captures."""
    out: dict[str, str] = {}
    for inc in ds.incidents:
        for k, cap in enumerate(inc.captures, start=1):
            out[cap.id] = inc.id if len(inc.captures) == 1 else f"{inc.id}#{k}"
    return out


def unit_map(ds: Dataset, spec: GroundTruthSpec) -> dict[str, str]:
    """Capture id -> scoring unit: the capture, or its incident when the spec collapses that incident."""
    incident = incident_of(ds)
    collapse = set(spec.collapse_incidents)
    return {c: (incident[c] if incident.get(c) in collapse else c) for c in incident}


def unit_groups(groups: list[list[str]], units: dict[str, str]) -> list[set[str]]:
    """Groups of captures (or of spec ids) as disjoint groups of units; overlapping groups are merged."""
    return merge_overlapping([{units.get(m, m) for m in g} for g in groups])


@dataclass(frozen=True)
class UnitPartition:
    clusters: list[list[str]]  # the engine's clusters over units; split units are in none
    split: list[str]  # units whose captures the engine placed in more than one cluster

    def index(self) -> dict[str, int]:
        return {u: n for n, members in enumerate(self.clusters) for u in members}


def unit_partition(clustered: Dataset, relation: str, ds: Dataset, units: dict[str, str]) -> UnitPartition:
    """The engine's clusters of one relation, projected onto the scoring units.

    Canonical captures only. A capture the engine left out is a cluster of its own. A unit is
    merged only when all its captures sit in one cluster; otherwise it is split and joins none.
    The result does not depend on the order in which the engine lists its clusters.
    """
    where: dict[str, int] = {}
    for n, members in enumerate(predicted_partition(clustered, relation)):
        for m in members:
            where[m] = n
    tokens: dict[str, set[object]] = defaultdict(set)
    for cid in _canonical_ids(ds):
        tokens[units.get(cid, cid)].add(where.get(cid, ("alone", cid)))
    clusters: dict[int, list[str]] = defaultdict(list)
    split: list[str] = []
    for unit, toks in tokens.items():
        if len(toks) > 1:
            split.append(unit)
        else:
            (tok,) = toks
            if isinstance(tok, int):
                clusters[tok].append(unit)
    return UnitPartition([sorted(clusters[n]) for n in sorted(clusters)], sorted(split))


def _label_groups(labels: dict[str, Any], level: str, units: dict[str, str]) -> list[set[str]]:
    return unit_groups([g["members"] for g in labels["groups"] if g["level"] == level], units)


def _label_pairs(labels: dict[str, Any], relation: str, units: dict[str, str]) -> tuple[list[tuple[str, str]], int]:
    """Must-not-link pairs of one relation as sorted unit pairs (distinct), and how many collapsed onto one unit."""
    pairs: set[tuple[str, str]] = set()
    same = 0
    for p in labels["must_not_link"]:
        if p["relation"] != relation:
            continue
        a, b = units.get(p["a"], p["a"]), units.get(p["b"], p["b"])
        if a == b:
            same += 1
        else:
            pairs.add((a, b) if a < b else (b, a))
    return sorted(pairs), same


def _evidence(clustered: Dataset, a_caps: set[str], b_caps: set[str]) -> list[dict[str, Any]]:
    """Evidence rows of the links between two units' captures: layer, kind, status and bits, never values."""
    rows: list[dict[str, Any]] = []
    for link in clustered.links:
        if (link.a in a_caps and link.b in b_caps) or (link.a in b_caps and link.b in a_caps):
            for e in link.evidence:
                rows.append({"link": link.id, "layer": _value(e.layer), "kind": e.kind,
                             "status": _value(e.status), "bits": e.bits})
    return rows or [{"note": "no direct link; the pair is joined through other captures"}]


def wrong_merges(clustered: Dataset, labels: dict[str, Any], parts: dict[str, UnitPartition],
                 levels: tuple[str, ...], units: dict[str, str], names: dict[str, str]) -> list[dict[str, Any]]:
    """Unit pairs the engine merged that the labels keep apart.

    One entry per (relation, level, pair). A pair that is both a platform-id wrong merge and an
    analyst must-not-link violation at the same relation is one entry whose `sources` lists both.
    """
    caps: dict[str, set[str]] = defaultdict(set)
    for c, u in units.items():
        caps[u].add(c)
    entries: dict[tuple[str, str, tuple[str, str]], dict[str, Any]] = {}

    def add(relation: str, level: str, pair: tuple[str, str], source: str) -> None:
        key = (relation, level, pair)
        entry = entries.get(key)
        if entry is None:
            a, b = pair
            entry = entries[key] = {
                "level": level, "relation": relation,
                "a": names.get(a, a), "b": names.get(b, b),
                "decision": "merge", "expected": "apart", "sources": [],
                "evidence": _evidence(clustered, caps[a], caps[b]),
            }
        if source not in entry["sources"]:
            entry["sources"].append(source)

    for level in levels:
        relation = PLATFORM_LEVELS[level][1].value
        truth: dict[str, int] = {}
        for n, group in enumerate(_label_groups(labels, level, units)):
            for m in group:
                truth[m] = n
        for cluster in parts[relation].clusters:
            members = [m for m in cluster if m in truth]
            for a, b in combinations(members, 2):
                if truth[a] != truth[b]:
                    add(relation, level, (a, b), "platform_ids")

    for relation in sorted({PLATFORM_LEVELS[lv][1].value for lv in levels}):
        where = parts[relation].index()
        for a, b in _label_pairs(labels, relation, units)[0]:
            if a in where and where[a] == where.get(b):
                attached = [k for k in entries if k[0] == relation and k[2] == (a, b)]
                if attached:
                    for key in attached:
                        add(key[0], key[1], key[2], "analyst_verdict")
                else:
                    add(relation, ANALYST_LEVEL, (a, b), "analyst_verdict")
    return [entries[k] for k in sorted(entries)]


def score_levels(labels: dict[str, Any], parts: dict[str, UnitPartition], units: dict[str, str],
                 levels: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every level with label groups, scored on the scoring units against its relation's partition."""
    rows = []
    for level in sorted(levels):
        groups = _label_groups(labels, level, units)
        if not groups:
            continue
        relation = PLATFORM_LEVELS[level][1].value
        truth = [sorted(g) for g in groups]
        pred = parts[relation].clusters
        universe = {m for t in truth for m in t}
        pairs, _ = _label_pairs(labels, relation, units)
        rows.append({
            "level": level,
            "relation": relation,
            "label_groups": len(truth),
            "label_groups_non_singleton": sum(1 for t in truth if len(t) > 1),
            "pairwise": pairwise(pred, truth),
            "bcubed": bcubed(pred, truth),
            "macro": macro_bcubed(pred, truth),
            "predicted_clusters_touching": len({i for i, c in enumerate(pred) if universe & set(c)}),
            "must_not_link": must_not_link(pred, pairs),
        })
    return rows


def informative_cases(parts: dict[str, UnitPartition], spec: GroundTruthSpec, units: dict[str, str],
                      levels: tuple[str, ...], names: dict[str, str]) -> list[dict[str, Any]]:
    """Unit pairs whose expected answer differs between levels, with what the engine answers at each level."""
    groups = {lv: unit_groups(spec.expected_e1.get(lv, []), units) for lv in levels}
    universe = {lv: set().union(*groups[lv]) for lv in levels}
    index = {lv: parts[PLATFORM_LEVELS[lv][1].value].index() for lv in levels}
    pairs: set[tuple[str, str]] = set()
    for lv in levels:
        for g in groups[lv]:
            pairs |= {(a, b) for a, b in combinations(sorted(g), 2)}
    rows: list[dict[str, Any]] = []
    for a, b in sorted(pairs):
        per: dict[str, dict[str, Any]] = {}
        for lv in levels:
            if a in universe[lv] and b in universe[lv]:
                expected = any(a in g and b in g for g in groups[lv])
                ca, cb = index[lv].get(a), index[lv].get(b)
                got = ca is not None and ca == cb
                per[lv] = {"expected": "together" if expected else "apart",
                           "engine": "together" if got else "apart", "agree": expected == got}
        if len({v["expected"] for v in per.values()}) > 1:
            rows.append({"a": names.get(a, a), "b": names.get(b, b), "levels": per})
    return rows


def run_hidden(ds: Dataset, spec: GroundTruthSpec, engine_name: str) -> dict[str, Any]:
    """E1 for one engine: leak check, link the hidden dataset, score every E1 level on the scoring units."""
    labels = build_labels(ds, spec)
    hidden = hidden_dataset(ds)
    clustered = get_engine(engine_name).link_dataset(hidden)
    units = unit_map(ds, spec)
    names = folder_names(ds)
    relations = sorted({PLATFORM_LEVELS[lv][1].value for lv in E1_LEVELS})
    parts = {rel: unit_partition(clustered, rel, ds, units) for rel in relations}
    return {
        "experiment": "E1 hidden-layer recovery",
        "engine": engine_name,
        "dataset": ds.name,
        "indicators_total": len(ds.indicators()),
        "indicators_hidden": len(ds.indicators()) - len(hidden.indicators()),
        "indicators_used": len(hidden.indicators()),
        "leak_check_hits": 0,
        "scoring_units": {
            "definition": UNIT_DEFINITION,
            "collapsed_incidents": sorted(spec.collapse_incidents),
            "units": len({units.get(c, c) for c in _canonical_ids(ds)}),
            "split_units": {rel: {"count": len(p.split), "units": [names.get(u, u) for u in p.split]}
                            for rel, p in parts.items()},
            "must_not_link_same_unit": {rel: _label_pairs(labels, rel, units)[1] for rel in relations},
        },
        "levels": score_levels(labels, parts, units, E1_LEVELS),
        "informative_cases": informative_cases(parts, spec, units, E1_LEVELS, names),
        "wrong_merges": wrong_merges(clustered, labels, parts, E1_LEVELS, units, names),
    }


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def render_markdown(result: dict[str, Any]) -> str:
    units = result["scoring_units"]
    split = ", ".join(f"{rel} {info['count']}" for rel, info in units["split_units"].items())
    lines = [
        f"# E1 hidden-layer recovery, engine `{result['engine']}`",
        "",
        f"Dataset `{result['dataset']}`: {result['indicators_total']} indicators, "
        f"{result['indicators_hidden']} platform-id indicators hidden, {result['indicators_used']} used. "
        f"Leak check: {result['leak_check_hits']} hits.",
        "",
        f"Scoring units: {units['units']} (collapsed incidents: {len(units['collapsed_incidents'])}; "
        f"split units by relation: {split}). Unit mapping: {units['definition']}",
        "",
        "| level | relation | label groups (non-singleton) | pairwise P (tp/pred) | pairwise R (tp/true) | "
        "B-cubed P | B-cubed R | macro P | macro R | must-not-link violated |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["levels"]:
        pw, b3, mac, mnl = row["pairwise"], row["bcubed"], row["macro"], row["must_not_link"]
        lines.append(
            f"| {row['level']} | {row['relation']} | {row['label_groups']} ({row['label_groups_non_singleton']}) | "
            f"{pw['tp']}/{pw['predicted_pairs']} | {pw['tp']}/{pw['true_pairs']} | {_f(b3['precision'])} | "
            f"{_f(b3['recall'])} | {_f(mac['precision'])} | {_f(mac['recall'])} | {mnl['violated']}/{mnl['pairs']} |"
        )
    lines += ["", f"Wrong merges (unit pairs merged that the labels keep apart): {len(result['wrong_merges'])}", ""]
    counts = Counter(w["level"] for w in result["wrong_merges"])
    lines += [f"- {lv}: {counts.get(lv, 0)}" for lv in (*E1_LEVELS, ANALYST_LEVEL)]
    lines += ["", "## Informative cases (expected answer differs between levels)", "",
              "| pair | level | expected | engine | agree |", "|---|---|---|---|---|"]
    for case in result["informative_cases"]:
        for lv, v in case["levels"].items():
            lines.append(f"| {case['a']} vs {case['b']} | {lv} | {v['expected']} | {v['engine']} | {v['agree']} |")
    if not result["informative_cases"]:
        lines.append("| (none in the private spec) | | | | |")
    return "\n".join(lines) + "\n"


def register(subparsers: argparse._SubParsersAction) -> None:
    from ftf.evaluate.commands import add_common, load_dataset, write_json  # noqa: PLC0415
    from ftf.evaluate.units import load_spec  # noqa: PLC0415

    p = subparsers.add_parser("hidden", help="E1: hide the platform-id layer, link, score campaign/owner/tenant")
    add_common(p)
    p.add_argument("--engine", default="baseline", help="engine name: baseline | linking (default: %(default)s)")

    def run(args: argparse.Namespace) -> int:
        ds = load_dataset(args)
        result = run_hidden(ds, load_spec(args.spec), args.engine)
        result["dataset_build"] = dataset_build(args.dataset)
        out = Path(args.out_dir) / "hidden"
        write_json(out / f"hidden-{args.engine}.json", result)
        md = out / f"hidden-{args.engine}.md"
        md.write_text(render_markdown(result), encoding="utf-8")
        print(f"wrote {md}")
        print(f"engine {args.engine}: {result['indicators_used']} indicators used, leak check 0 hits")
        for row in result["levels"]:
            pw, b3, mac, mnl = row["pairwise"], row["bcubed"], row["macro"], row["must_not_link"]
            print(
                f"{row['relation']:>9} vs {row['level']:<9} pairwise P {pw['tp']}/{pw['predicted_pairs']} "
                f"R {pw['tp']}/{pw['true_pairs']}  B3 P {_f(b3['precision'])} R {_f(b3['recall'])} "
                f"(macro P {_f(mac['precision'])} R {_f(mac['recall'])}) must-not-link violated {mnl['violated']}/{mnl['pairs']}"
            )
        for rel, info in result["scoring_units"]["split_units"].items():
            print(f"split units ({rel}): {info['count']}")
        print(f"wrong merges {len(result['wrong_merges'])}, informative cases {len(result['informative_cases'])}")
        return 0

    p.set_defaults(func=run)
