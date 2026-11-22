"""E7 time split: build the reference from the 8 October captures, assign the later incidents one at a time.

Reference (the base) = incidents whose latest capture date is on or before 2026-10-08. Capture
dates come from the dataset's `captured_on` metadata only, never from file names. An incident
with no capture date falls back to the organiser set (BACKUP is the reference, do_udostepnienia
is later), and the output says which incidents took that fallback.

Each later incident is assigned against the reference with `engine.link_incident(base, incident,
relation=campaign)`. The base is not grown between items, so every decision is an open-set
answer against the reference only. The expected answer comes from the ground-truth campaign
groups (platform ids and analyst verdicts, merged where they share a folder): assign to the
reference folders in the same group, or new when no reference folder is in it.

Folders are the scoring unit (`do_udostepnienia:61`), so the output never prints a domain.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ftf.contract import Dataset, Relation
from ftf.evaluate.engine import get_engine
from ftf.evaluate.hidden import hidden_dataset
from ftf.evaluate.labels import build_labels
from ftf.evaluate.metrics import openset_counts
from ftf.evaluate.units import GroundTruthSpec

REFERENCE_DATE = "2026-10-08"
REFERENCE_SOURCE = "BACKUP"  # organiser set used only for incidents without any capture date
RELATION = Relation.campaign


def split_incidents(ds: Dataset, reference_date: str = REFERENCE_DATE) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
    """(reference incident ids, later incident ids, per-incident split info), from capture metadata."""
    ref: list[str] = []
    later: list[str] = []
    info: dict[str, dict[str, Any]] = {}
    for inc in ds.incidents:
        dates = sorted(c.captured_on for c in inc.captures if c.captured_on)
        date = dates[-1] if dates else None
        if date is not None:
            side = "reference" if date <= reference_date else "later"
            how = "capture date"
        else:
            side = "reference" if inc.source == REFERENCE_SOURCE else "later"
            how = f"organiser set {inc.source} (no capture date)"
        info[inc.id] = {"date": date, "side": side, "split_by": how, "source": inc.source}
        (ref if side == "reference" else later).append(inc.id)
    later.sort(key=lambda i: (info[i]["date"] or "", i))
    return ref, later, info


def _subset(ds: Dataset, ids: list[str]) -> Dataset:
    """The dataset restricted to some incidents, with no links, campaigns or assignments."""
    keep = set(ids)
    data = ds.model_copy(update={
        "incidents": [i for i in ds.incidents if i.id in keep],
        "links": [], "campaigns": [], "assignments": [],
    })
    return Dataset.model_validate(data.model_dump(mode="json"))


def _merge_overlapping(groups: list[set[str]]) -> list[set[str]]:
    """Union of sets that share any member (the output sets are disjoint)."""
    out: list[set[str]] = []
    for g in groups:
        g = set(g)
        keep = []
        for h in out:
            if h & g:
                g |= h
            else:
                keep.append(h)
        out = keep + [g]
    return out


def campaign_truth(labels: dict[str, Any], folder: dict[str, str]) -> list[set[str]]:
    """Ground-truth campaign groups as sets of folder ids (platform-id and analyst campaign groups merged)."""
    groups = [{folder[m] for m in g["members"] if m in folder}
              for g in labels["groups"] if g["relation"] == RELATION.value]
    return _merge_overlapping([g for g in groups if g])


def _verdict(decision: str, expected: list[str], assigned: list[str] | None) -> str:
    if decision == "assign":
        if not expected:
            return "false accept"
        return "correct assignment" if assigned and set(expected) & set(assigned) else "wrong assignment"
    if decision == "new":
        return "correct new" if not expected else "missed (new, should assign)"
    return "review"


def run_timesplit(ds: Dataset, spec: GroundTruthSpec, engine_name: str, hide: bool = True) -> dict[str, Any]:
    """E7 for one engine: reference base, then each later incident assigned one at a time."""
    engine = get_engine(engine_name)
    labels = build_labels(ds, spec)
    folder = {c.id: inc.id for inc in ds.incidents for c in inc.captures}
    work = hidden_dataset(ds) if hide else ds
    ref_ids, later_ids, info = split_incidents(ds)
    base = engine.link_dataset(_subset(work, ref_ids))
    incidents = {i.id: i for i in work.incidents}
    camp_members = {
        c.id: sorted({folder[m] for m in c.capture_ids if m in folder})
        for c in base.campaigns if c.relation == RELATION
    }
    truth = campaign_truth(labels, folder)
    ref_set = set(ref_ids)

    items: list[dict[str, Any]] = []
    outcomes: list[tuple[set[str] | None, str, set[str] | None]] = []
    for item in later_ids:
        group = next((t for t in truth if item in t), set())
        expected = sorted((group - {item}) & ref_set)
        result = engine.link_incident(base, incidents[item], relation=RELATION)
        a = result.assignment
        decision = _value(a.decision)
        assigned = camp_members.get(a.campaign_id, []) if decision == "assign" and a.campaign_id else None
        outcomes.append((set(expected) or None, decision, set(assigned) if assigned else None))
        verdict = _verdict(decision, expected, assigned)
        if expected:
            why = f"its ground-truth campaign group holds reference folder(s) {', '.join(expected)}"
        else:
            why = "no reference folder is in its ground-truth campaign group"
        if info[item]["split_by"] != "capture date":
            why += f"; split by {info[item]['split_by']}"
        items.append({
            "item": item,
            "date": info[item]["date"],
            "decision": decision,
            "assigned_to": assigned,
            "top_score_bits": a.candidates[0].score_bits if a.candidates else None,
            "expected": f"assign to {', '.join(expected)}" if expected else "new",
            "why": why,
            "verdict": verdict,
        })
    return {
        "experiment": "E7 time split",
        "engine": engine_name,
        "dataset": ds.name,
        "platform_ids": "hidden" if hide else "visible",
        "reference_date": REFERENCE_DATE,
        "reference": [{"folder": r, "date": info[r]["date"], "split_by": info[r]["split_by"]} for r in ref_ids],
        "later": len(later_ids),
        "fallback_incidents": sorted(i for i in info if info[i]["split_by"] != "capture date"),
        "counts": openset_counts(outcomes),
        "items": items,
        "wrong_or_false": [i for i in items if i["verdict"] in ("wrong assignment", "false accept")],
    }


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


def render_markdown(result: dict[str, Any]) -> str:
    c = result["counts"]
    lines = [
        f"# E7 time split, engine `{result['engine']}`, platform ids {result['platform_ids']}",
        "",
        f"Reference: {len(result['reference'])} incidents dated on or before {result['reference_date']} "
        f"(organiser-set fallback for {len(result['fallback_incidents'])} without a date). "
        f"Later incidents assigned one at a time: {result['later']}.",
        "",
        f"Counts: items {c.get('items', 0)}, correct assignments {c.get('correct', 0)}, "
        f"wrong assignments {c.get('wrong', 0)}, false accepts {c.get('false_accepts', 0)}, "
        f"new {c.get('new', 0)} (correct new {c.get('new_correct', 0)}), review {c.get('review', 0)}.",
        "",
        "| item | date | decision | assigned to | expected | why | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in result["items"]:
        assigned = ", ".join(it["assigned_to"]) if it["assigned_to"] else "-"
        lines.append(f"| {it['item']} | {it['date'] or '-'} | {it['decision']} | {assigned} | "
                     f"{it['expected']} | {it['why']} | {it['verdict']} |")
    return "\n".join(lines) + "\n"


def register(subparsers: argparse._SubParsersAction) -> None:
    from ftf.evaluate.commands import add_common, load_dataset, write_json  # noqa: PLC0415
    from ftf.evaluate.units import load_spec  # noqa: PLC0415

    p = subparsers.add_parser("timesplit", help="E7: reference = 8 October captures; assign later incidents one at a time")
    add_common(p)
    p.add_argument("--engine", default="baseline", help="engine name: baseline | linking (default: %(default)s)")
    p.add_argument("--hide", action=argparse.BooleanOptionalAction, default=True,
                   help="hide platform ids before linking (default: on)")

    def run(args: argparse.Namespace) -> int:
        ds = load_dataset(args)
        result = run_timesplit(ds, load_spec(args.spec), args.engine, hide=args.hide)
        out = Path(args.out_dir) / "timesplit"
        mode = "hidden" if args.hide else "visible"
        write_json(out / f"timesplit-{args.engine}-{mode}.json", result)
        md = out / f"timesplit-{args.engine}-{mode}.md"
        md.write_text(render_markdown(result), encoding="utf-8")
        print(f"wrote {md}")
        c = result["counts"]
        print(f"engine {args.engine}, platform ids {mode}: reference {len(result['reference'])} incidents, "
              f"later {result['later']}, fallback {len(result['fallback_incidents'])}")
        print(f"items {c.get('items', 0)} correct {c.get('correct', 0)} wrong {c.get('wrong', 0)} "
              f"false_accepts {c.get('false_accepts', 0)} new {c.get('new', 0)} new_correct {c.get('new_correct', 0)} "
              f"review {c.get('review', 0)}")
        for it in result["items"]:
            print(f"  {it['item']}: {it['decision']} (expected {it['expected']}) -> {it['verdict']}")
        return 0

    p.set_defaults(func=run)
