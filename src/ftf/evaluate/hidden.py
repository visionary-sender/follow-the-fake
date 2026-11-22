"""E1 hidden-layer recovery: link the dataset with the platform-id layer hidden.

`hidden_dataset` removes the layer (`ftf.evaluate.leak.hide_platform_ids`) and refuses to go on
unless `leak_check` finds 0 hits. The engine then links the hidden dataset, and each relation's
partition is scored against the ground-truth groups of the levels it stands for: campaign
(relation campaign), owner and tenant (relation operator).

Informative cases are pairs that the spec's expected E1 groups put together at one level and
apart at another. The expectations are read from the private spec, never from this module.
Folders are shown as case-folder ids (`do_udostepnienia:61`, `BACKUP:p3#2` for the second capture
of a folder); domains and other organiser values are never printed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from ftf.contract import Dataset
from ftf.evaluate.engine import get_engine
from ftf.evaluate.labels import PLATFORM_LEVELS, build_labels
from ftf.evaluate.leak import hide_platform_ids, leak_check
from ftf.evaluate.metrics import predicted_partition, score_against_labels
from ftf.evaluate.units import GroundTruthSpec, incident_of

E1_LEVELS = ("campaign", "owner", "tenant")


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


def _capture_ids(ds: Dataset) -> set[str]:
    return {c.id for inc in ds.incidents for c in inc.captures}


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


def _evidence(clustered: Dataset, a: str, b: str) -> list[dict[str, Any]]:
    """Evidence rows of the links between two captures: layer, kind, status and bits, never values."""
    rows: list[dict[str, Any]] = []
    for link in clustered.links:
        if {link.a, link.b} == {a, b}:
            for e in link.evidence:
                rows.append({"link": link.id, "layer": _value(e.layer), "kind": e.kind,
                             "status": _value(e.status), "bits": e.bits})
    return rows or [{"note": "no direct link; the pair is joined through other captures"}]


def wrong_merges(clustered: Dataset, labels: dict[str, Any], levels: tuple[str, ...], names: dict[str, str]) -> list[dict[str, Any]]:
    """Merges the labels keep apart: platform-id groups, and analyst must-not-link pairs (their reasons are not printed)."""
    out: list[dict[str, Any]] = []
    for level in levels:
        relation = PLATFORM_LEVELS[level][1].value
        truth: dict[str, int] = {}
        for n, g in enumerate(x for x in labels["groups"] if x["level"] == level):
            for m in g["members"]:
                truth[m] = n
        for cluster in predicted_partition(clustered, relation):
            members = sorted(m for m in cluster if m in truth)
            for a, b in combinations(members, 2):
                if truth[a] != truth[b]:
                    out.append({"level": level, "relation": relation, "source": "platform_ids",
                                "a": names.get(a, a), "b": names.get(b, b),
                                "decision": "merge", "expected": "apart", "evidence": _evidence(clustered, a, b)})
    for relation in sorted({PLATFORM_LEVELS[lv][1].value for lv in levels}):
        where: dict[str, int] = {}
        for n, cluster in enumerate(predicted_partition(clustered, relation)):
            for m in cluster:
                where.setdefault(m, n)
        for pair in labels["must_not_link"]:
            a, b = pair["a"], pair["b"]
            if pair["relation"] == relation and a in where and where.get(a) == where.get(b):
                out.append({"level": "analyst_must_not_link", "relation": relation, "source": "analyst_verdict",
                            "a": names.get(a, a), "b": names.get(b, b), "decision": "merge", "expected": "apart",
                            "evidence": _evidence(clustered, a, b)})
    return out


def _unit_map(ds: Dataset, spec: GroundTruthSpec) -> dict[str, str]:
    """Capture id -> scoring unit: the capture, or its incident when the spec collapses that incident."""
    incident = incident_of(ds)
    collapse = set(spec.collapse_incidents)
    return {c: (incident[c] if incident.get(c) in collapse else c) for c in incident}


def _engine_clusters(clustered: Dataset, relation: str, units: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n, members in enumerate(predicted_partition(clustered, relation)):
        for m in members:
            out.setdefault(units.get(m, m), n)
    return out


def informative_cases(clustered: Dataset, spec: GroundTruthSpec, ds: Dataset, levels: tuple[str, ...],
                      names: dict[str, str]) -> list[dict[str, Any]]:
    """Pairs whose expected answer differs between levels, with what the engine answers at each level."""
    units = _unit_map(ds, spec)
    groups = {lv: [{units.get(m, m) for m in g} for g in spec.expected_e1.get(lv, [])] for lv in levels}
    universe = {lv: set().union(*groups[lv]) if groups[lv] else set() for lv in levels}
    engine = {lv: _engine_clusters(clustered, PLATFORM_LEVELS[lv][1].value, units) for lv in levels}
    pairs: set[tuple[str, str]] = set()
    for lv in levels:
        for g in groups[lv]:
            pairs |= {tuple(sorted(p)) for p in combinations(sorted(g), 2)}  # type: ignore[misc]
    rows: list[dict[str, Any]] = []
    for a, b in sorted(pairs):
        per: dict[str, dict[str, Any]] = {}
        for lv in levels:
            if a in universe[lv] and b in universe[lv]:
                expected = any(a in g and b in g for g in groups[lv])
                ca, cb = engine[lv].get(a), engine[lv].get(b)
                got = ca is not None and ca == cb
                per[lv] = {"expected": "together" if expected else "apart",
                           "engine": "together" if got else "apart", "agree": expected == got}
        if len({v["expected"] for v in per.values()}) > 1:
            rows.append({"a": names.get(a, a), "b": names.get(b, b), "levels": per})
    return rows


def run_hidden(ds: Dataset, spec: GroundTruthSpec, engine_name: str) -> dict[str, Any]:
    """E1 for one engine: leak check, link the hidden dataset, score every E1 level."""
    labels = build_labels(ds, spec)
    hidden = hidden_dataset(ds)
    clustered = get_engine(engine_name).link_dataset(hidden)
    scored_labels = {**labels, "groups": [g for g in labels["groups"] if g["level"] in E1_LEVELS]}
    names = folder_names(ds)
    return {
        "experiment": "E1 hidden-layer recovery",
        "engine": engine_name,
        "dataset": ds.name,
        "indicators_total": len(ds.indicators()),
        "indicators_hidden": len(ds.indicators()) - len(hidden.indicators()),
        "indicators_used": len(hidden.indicators()),
        "leak_check_hits": 0,
        "levels": score_against_labels(clustered, scored_labels)["levels"],
        "informative_cases": informative_cases(clustered, spec, ds, E1_LEVELS, names),
        "wrong_merges": wrong_merges(clustered, labels, E1_LEVELS, names),
    }


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# E1 hidden-layer recovery, engine `{result['engine']}`",
        "",
        f"Dataset `{result['dataset']}`: {result['indicators_total']} indicators, "
        f"{result['indicators_hidden']} platform-id indicators hidden, {result['indicators_used']} used. "
        f"Leak check: {result['leak_check_hits']} hits.",
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
    lines += ["", f"Wrong merges (pairs merged that the labels keep apart): {len(result['wrong_merges'])}", ""]
    counts = Counter(w["level"] for w in result["wrong_merges"])
    lines += [f"- {lv}: {counts.get(lv, 0)}" for lv in (*E1_LEVELS, "analyst_must_not_link")]
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
        print(f"wrong merges {len(result['wrong_merges'])}, informative cases {len(result['informative_cases'])}")
        return 0

    p.set_defaults(func=run)


