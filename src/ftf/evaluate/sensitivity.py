"""`ftf eval sensitivity` (E6): does a decision survive moving one weight of the Linking table?

Every weight of `ftf.linking.weights.HAND_TABLE` that the dataset reaches is moved by −2, −1, +1
and +2 bits, one at a time: a kind the dataset uses, or a layer default that a kind outside the
table falls back to. For each move the Linking engine is rebuilt with the moved table and compared
with the unperturbed run on two kinds of decision:

- pair links per relation (`ftf.linking.link_dataset`): a pair linked in one run and not in the
  other is one flip;
- the E7 verdicts (`ftf.linking.link_incident` for every later incident against the reference
  base, the base rebuilt with the same table): an item whose decision (assign / new / review) or
  assigned reference folders change is one flip.

`ftf.linking.sensitivity.fragile_links` adds the per-link view: the links whose verdict changes
under a ±2 move of one layer, or only when every layer moves together.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from ftf.contract import Dataset, Layer, Relation
from ftf.evaluate.hidden import dataset_build, hidden_dataset
from ftf.evaluate.timesplit import split_incidents
from ftf.evaluate.units import incident_of
from ftf.linking import link_dataset, link_incident
from ftf.linking.backend import Backend
from ftf.linking.grouping import ALL_RELATIONS
from ftf.linking.ledger import ScoringContext
from ftf.linking.sensitivity import DELTA, fragile_links
from ftf.linking.weights import HAND_TABLE, WeightTable

SCHEMA = "ftf-eval-sensitivity@0.1"
MOVES: tuple[float, ...] = (-2.0, -1.0, 1.0, 2.0)
RELATION = Relation.campaign


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


def _move_name(delta: float) -> str:
    return f"{delta:+g}"


def weight_keys(ds: Dataset, table: WeightTable = HAND_TABLE) -> list[str]:
    """Weights the dataset can reach: `kind:<name>` for table kinds it uses, and `layer_default:<layer>`
    for layers whose indicators carry a kind the table does not list (they fall back to the default)."""
    used = [(_value(i.kind), _value(i.layer)) for i in ds.indicators()]
    keys = [f"kind:{k}" for k in sorted(table.kinds) if any(kind == k for kind, _ in used)]
    unlisted = {layer for kind, layer in used if kind not in table.kinds}
    defaults = sorted(l.value for l in table.layer_defaults if l.value in unlisted)
    return keys + [f"layer_default:{name}" for name in defaults]


def bits_of(table: WeightTable, key: str) -> float:
    kind, _, name = key.partition(":")
    if kind == "kind":
        return table.kinds[name].bits
    return table.layer_defaults[Layer(name)]


def move(table: WeightTable, key: str, delta: float) -> WeightTable:
    """The table with one weight moved by `delta` bits, floored at 0 bits."""
    kind, _, name = key.partition(":")
    if kind == "kind":
        entry = table.kinds[name]
        return replace(table, kinds={**table.kinds, name: replace(entry, bits=max(entry.bits + delta, 0.0))})
    layer = Layer(name)
    return replace(table, layer_defaults={**table.layer_defaults, layer: max(table.layer_defaults[layer] + delta, 0.0)})


def effective_delta(table: WeightTable, key: str, delta: float) -> float:
    """The change in bits that `move` really makes (after − before). It is smaller in size than `delta`
    when the 0-bit floor clamps the move."""
    return round(bits_of(move(table, key, delta), key) - bits_of(table, key), 6)


def _move_cell(move_result: dict[str, Any], nominal: float) -> str:
    """Flip count for one move; a clamped move also shows the change it really made, e.g. `3 (eff. -1)`."""
    if abs(move_result["effective"] - nominal) < 1e-9:
        return str(move_result["total"])
    return f"{move_result['total']} (eff. {move_result['effective']:+g})"


def _subset(ds: Dataset, incident_ids: list[str]) -> Dataset:
    keep = set(incident_ids)
    data = ds.model_dump(mode="json")
    data["incidents"] = [i for i in data["incidents"] if i["id"] in keep]
    data["links"], data["campaigns"], data["assignments"] = [], [], []
    return Dataset.model_validate(data)


def _e7_verdicts(ds: Dataset, weights: WeightTable) -> dict[str, str]:
    """Each later incident's E7 verdict under `weights`: `new`, `review`, or `assign <reference folders>`."""
    ref_ids, later_ids, _ = split_incidents(ds)
    base_ds = _subset(ds, ref_ids)
    base = link_dataset(base_ds, backend=Backend.from_context(ScoringContext.from_dataset(base_ds, weights)))
    folder = incident_of(ds)
    members = {
        c.id: sorted({folder[m] for m in c.capture_ids if m in folder})
        for c in base.campaigns if c.relation is RELATION
    }
    incidents = {i.id: i for i in ds.incidents}
    out: dict[str, str] = {}
    for item in later_ids:
        a = link_incident(base, incidents[item], RELATION, backend=Backend.from_dataset, weights=weights).assignment
        decision = _value(a.decision)
        if decision == "assign" and members.get(a.campaign_id):
            out[item] = f"assign {', '.join(members[a.campaign_id])}"
        else:
            out[item] = decision
    return out


def decisions(ds: Dataset, weights: WeightTable = HAND_TABLE) -> tuple[Dataset, dict[str, Any]]:
    """The Linking engine's decisions on `ds` under `weights`: the linked dataset, the pair links
    as (relation, a, b) triples, and the E7 verdict of every later incident."""
    linked = link_dataset(ds, backend=Backend.from_context(ScoringContext.from_dataset(ds, weights)))
    pairs = {(l.relation.value, *sorted((l.a, l.b))) for l in linked.links}
    return linked, {"pairs": pairs, "e7": _e7_verdicts(ds, weights)}


def compare(base: dict[str, Any], moved: dict[str, Any]) -> dict[str, Any]:
    """Flipped pair links per relation and changed E7 verdicts between two runs."""
    per_relation = Counter(rel for rel, _, _ in base["pairs"] ^ moved["pairs"])
    e7 = sum(base["e7"][item] != moved["e7"].get(item) for item in base["e7"])
    pair_total = sum(per_relation.values())
    return {
        "pair_flips": dict(sorted(per_relation.items())),
        "pair_flips_total": pair_total,
        "e7_flips": e7,
        "total": pair_total + e7,
    }


def _baseline_counts(decided: dict[str, Any]) -> dict[str, Any]:
    pairs = Counter(rel for rel, _, _ in decided["pairs"])
    e7 = Counter(v.split(" ", 1)[0] for v in decided["e7"].values())
    return {
        "pair_links": {r.value: pairs.get(r.value, 0) for r in ALL_RELATIONS},
        "e7": {k: e7.get(k, 0) for k in ("assign", "new", "review")},
        "later_items": len(decided["e7"]),
    }


def run_sensitivity(ds: Dataset, weights: WeightTable = HAND_TABLE, moves: tuple[float, ...] = MOVES,
                    delta: float = DELTA) -> dict[str, Any]:
    """E6 on `ds` (the dataset the engine should see, already with platform ids hidden if wanted)."""
    linked, base = decisions(ds, weights)
    keys = weight_keys(ds, weights)
    ranking = []
    for key in keys:
        by_move = {}
        for d in moves:
            _, moved = decisions(ds, move(weights, key, d))
            by_move[_move_name(d)] = {**compare(base, moved), "effective": effective_delta(weights, key, d)}
        relations: Counter[str] = Counter()
        for m in by_move.values():
            relations.update(m["pair_flips"])
        ranking.append({
            "weight": key,
            "bits": bits_of(weights, key),
            "flips_total": sum(m["total"] for m in by_move.values()),
            "pair_flips_total": sum(m["pair_flips_total"] for m in by_move.values()),
            "e7_flips_total": sum(m["e7_flips"] for m in by_move.values()),
            "pair_flips_by_relation": dict(sorted(relations.items())),
            "by_move": by_move,
        })
    ranking.sort(key=lambda r: (-r["flips_total"], r["weight"]))
    records = fragile_links(linked, delta=delta, weights=weights)
    by_relation: dict[str, dict[str, int]] = {}
    for r in records:
        s = by_relation.setdefault(r["relation"], {"links": 0, "fragile": 0, "worst_case": 0})
        s["links"] += 1
        s["fragile"] += bool(r["fragile"])
        s["worst_case"] += r["worst_case"] is not None
    return {
        "schema": SCHEMA,
        "experiment": "E6 sensitivity",
        "engine": "linking",
        "dataset": ds.name,
        "moves_bits": list(moves),
        "weights_in_table": len(weights.kinds) + len(weights.layer_defaults),
        "weights_tested": len(keys),
        "baseline": _baseline_counts(base),
        "ranking": ranking,
        "fragile": {
            "delta_bits": delta,
            "links": len(records),
            "fragile": sum(bool(r["fragile"]) for r in records),
            "worst_case_only": sum(r["worst_case"] is not None and not r["fragile"] for r in records),
            "by_relation": dict(sorted(by_relation.items())),
            "records": records,
        },
    }


def incident_pairs(records: list[dict[str, Any]], folder: dict[str, str]) -> list[dict[str, Any]]:
    """Fragile-link records with their endpoints as case folders (incident ids), for the report."""
    out = []
    for r in records:
        a, b = folder.get(r["a"], "?"), folder.get(r["b"], "?")
        out.append({"relation": r["relation"], "a": a, "b": b, "state": r["state"], "bits": r["bits"],
                    "tau": r["tau"], "fragile": r["fragile"], "flips": r["flips"], "worst_case": r["worst_case"]})
    return out


def render_markdown(result: dict[str, Any], folder: dict[str, str]) -> str:
    lines = [
        "# E6 sensitivity (linking table)", "",
        f"Moves: {', '.join(f'{m:+g}' for m in result['moves_bits'])} bits, one weight at a time. "
        f"Weights tested: {result['weights_tested']} of {result['weights_in_table']} in the table.", "",
        "| weight | bits | −2 | −1 | +1 | +2 | total flips |", "|---|---|---|---|---|---|---|",
    ]
    for r in result["ranking"]:
        cells = [_move_cell(r["by_move"][_move_name(d)], d) for d in result["moves_bits"]]
        lines.append(f"| {r['weight']} | {r['bits']:g} | " + " | ".join(cells) + f" | {r['flips_total']} |")
    f = result["fragile"]
    lines += ["", f"Fragile links (±{f['delta_bits']:g} bits, one layer): {f['fragile']} of {f['links']} pair links; "
              f"{f['worst_case_only']} more flip only when every layer moves at once.", "",
              "| relation | incident a | incident b | state | fragile move |", "|---|---|---|---|---|"]
    for r in incident_pairs(f["records"], folder):
        if not r["fragile"]:
            continue
        moves = "; ".join(f"{x['layer']} {x['delta']:+g} → {x['to']}" for x in r["flips"])
        lines.append(f"| {r['relation']} | {r['a']} | {r['b']} | {r['state']} | {moves} |")
    return "\n".join(lines) + "\n"


def register(subparsers: argparse._SubParsersAction) -> None:
    from ftf.evaluate.commands import add_common, load_dataset, write_json  # noqa: PLC0415

    p = subparsers.add_parser("sensitivity", help="E6: move each linking weight by ±1, ±2 bits; count flipped decisions")
    add_common(p)
    p.add_argument("--hide", action=argparse.BooleanOptionalAction, default=True,
                   help="hide platform ids before linking (default: on)")

    def run(args: argparse.Namespace) -> int:
        ds = load_dataset(args)
        work = hidden_dataset(ds) if args.hide else ds
        result = run_sensitivity(work)
        result["platform_ids"] = "hidden" if args.hide else "visible"
        result["dataset_build"] = dataset_build(args.dataset)
        out = Path(args.out_dir) / "sensitivity"
        write_json(out / "sensitivity-linking.json", result)
        md = out / "sensitivity-linking.md"
        md.write_text(render_markdown(result, incident_of(ds)), encoding="utf-8")
        print(f"wrote {md}")
        top = [f"{r['weight']} {r['flips_total']}" for r in result["ranking"][:5]]
        b = result["baseline"]
        print(f"weights tested {result['weights_tested']}; baseline pair links {b['pair_links']}, E7 {b['e7']}")
        print("top flips: " + (", ".join(top) or "none"))
        f = result["fragile"]
        print(f"fragile links {f['fragile']} of {f['links']}; worst-case only {f['worst_case_only']}")
        return 0

    p.set_defaults(func=run)
