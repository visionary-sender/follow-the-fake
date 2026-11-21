"""CLI of the linking engine.

`ftf score IN.json A B [--relation R]` prints one pair's ledger; `ftf link` writes one partition
per relation; `ftf assign` assigns new incidents to a linked collection.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from ftf.contract.models import Dataset, Relation
from ftf.linking.assign import combine, link_incident
from ftf.linking.backend import Backend
from ftf.linking.grouping import ALL_RELATIONS, link_dataset
from ftf.linking.learn_commands import add_weights_argument, context_from_args
from ftf.linking.ledger import ScoringContext, level_of, score_pair, threshold, verdict
from ftf.linking.types import PairScore


def register_score(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "score",
        help="print the evidence ledger of two captures",
        description="Score two captures of a contract dataset and print the itemised evidence ledger "
        "(bits per layer, rarity, threshold and verdict). Reads only contract indicators.",
    )
    parser.add_argument("dataset", type=Path, metavar="IN.json", help="contract Dataset (JSON)")
    parser.add_argument("a", metavar="A", help="capture id")
    parser.add_argument("b", metavar="B", help="capture id")
    parser.add_argument(
        "--relation", choices=[r.value for r in Relation], default=Relation.campaign.value,
        help="relation to score (default: %(default)s)",
    )
    add_weights_argument(parser)
    parser.set_defaults(func=run_score)


def run_score(args: argparse.Namespace) -> int:
    try:
        ds = Dataset.from_json(args.dataset.read_bytes())
        ctx = context_from_args(ds, args)
        score = score_pair(ctx, args.a, args.b, Relation(args.relation))
    except (OSError, ValidationError, KeyError, ValueError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"ftf score: {msg}", file=sys.stderr)
        return 2
    print(format_ledger(score, ctx))
    return 0


def format_ledger(score: PairScore, ctx: ScoringContext) -> str:
    tau = threshold(ctx, score.relation)
    head = ["layer", "kind", "status", "bits", "seen in", "value", "note"]
    body = [
        [r.layer.value, r.kind, r.status.value, f"{r.bits:+.2f}", r.seen_in or "", r.value or "", r.note or ""]
        for r in score.rows
    ]
    widths = [max(len(x[i]) for x in [head] + body) for i in range(len(head) - 1)]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)) + "  " + cells[-1]

    out = [f"{score.a}  <->  {score.b}   relation={score.relation.value}", line(head), line(["-" * w for w in widths] + ["----"])]
    out += [line(r) for r in body] or ["(no evidence rows)"]
    out.append(
        f"total {score.bits:+.2f} bits   τ={tau:.2f} (n={ctx.n})   positive layers={score.layers_positive}   "
        f"live private id={'yes' if score.live_private else 'no'}   verdict={verdict(score, ctx)}   "
        f"level={level_of(score).value}"
    )
    return "\n".join(out)


def register_link(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "link",
        help="group captures into campaigns, one partition per relation",
        description="Score candidate capture pairs with the evidence ledger and write one partition per relation "
        "(links and campaigns) into the dataset. Existing links, campaigns and assignments are replaced.",
    )
    parser.add_argument("input", type=Path, metavar="IN.json", help="contract dataset")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="OUT.json")
    parser.add_argument(
        "--relation",
        action="append",
        choices=[r.value for r in Relation],
        help="relation to group under (repeatable; default: all)",
    )
    add_weights_argument(parser)
    parser.set_defaults(func=run_link)


def register_assign(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "assign",
        help="assign new incidents to existing campaigns, or declare them new, or send them to review",
        description="COLLECTION is the output of `ftf link`. Each incident of NEW is scored against the "
        "collection's campaigns (no re-clustering). OUT is the collection plus the new incidents, their "
        "assignments and the ledgers to their top candidates.",
    )
    parser.add_argument("collection", type=Path, metavar="COLLECTION.json")
    parser.add_argument("new", type=Path, metavar="NEW.json")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="OUT.json")
    parser.add_argument("--relation", choices=[r.value for r in Relation], default=Relation.campaign.value)
    add_weights_argument(parser)
    parser.set_defaults(func=run_assign)


def _load(path: Path) -> Dataset:
    return Dataset.from_json(path.read_bytes())


def _write(ds: Dataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ds.to_json(), encoding="utf-8")


def run_link(args: argparse.Namespace) -> int:
    ds = _load(args.input)
    relations = list(dict.fromkeys(Relation(r) for r in args.relation)) if args.relation else list(ALL_RELATIONS)
    out = link_dataset(ds, relations, backend=Backend.from_context(context_from_args(ds, args)))
    _write(out, args.out)
    n_caps = sum(len(i.captures) for i in out.incidents)
    print(f"{len(out.incidents)} incidents, {n_caps} captures -> {args.out}")
    for relation in relations:
        camps = [c for c in out.campaigns if c.relation is relation]
        groups = [c for c in camps if len({out.incident_of(x).id for x in c.capture_ids}) > 1]
        n_links = sum(len(c.link_ids) for c in camps)
        print(f"  {relation.value}: {len(camps)} campaigns ({len(groups)} spanning >1 incident), {n_links} links")
        for c in groups:
            print(f"    {c.id}: {c.label}")
    return 0


def run_assign(args: argparse.Namespace) -> int:
    base = _load(args.collection)
    new = _load(args.new)
    relation = Relation(args.relation)
    if not any(c.relation is relation for c in base.campaigns):
        print(f"ftf assign: {args.collection} has no {relation.value} campaigns; run `ftf link` with that relation first", file=sys.stderr)
        return 2
    # New incidents may duplicate each other's captures (`duplicate_of` points at the smallest id):
    # add them one at a time, each after the captures it points at, so each one validates.
    known = {c.id for i in base.incidents for c in i.captures}
    pending = list(new.incidents)
    results = []
    current = base
    while pending:
        ready = [i for i in pending if all(c.duplicate_of is None or c.duplicate_of in known or c.duplicate_of in {x.id for x in i.captures} for c in i.captures)]
        if not ready:
            missing = sorted({c.duplicate_of for i in pending for c in i.captures if c.duplicate_of and c.duplicate_of not in known})
            print(f"ftf assign: duplicate_of points at unknown captures: {', '.join(missing)}", file=sys.stderr)
            return 2
        for inc in ready:
            res = link_incident(current, inc, relation, backend=lambda d: Backend.from_context(context_from_args(d, args)))
            results.append(res)
            data = combine(current, [inc]).model_dump(mode="json")
            data["links"] += [l.model_dump(mode="json") for l in res.links]
            data["assignments"] += [res.assignment.model_dump(mode="json")]
            current = Dataset.model_validate(data)
            known |= {c.id for c in inc.captures}
            pending.remove(inc)
    out = current
    _write(out, args.out)
    counts = Counter(r.assignment.decision.value for r in results)
    print(f"{len(results)} incidents ({relation.value}): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" -> {args.out}")
    for r in results:
        a = r.assignment
        top = a.candidates[0] if a.candidates else None
        best = f"{top.campaign_id} {top.score_bits:+.1f} bits" if top else "no candidate"
        target = f" -> {a.campaign_id}" if a.campaign_id else ""
        print(f"  {a.incident_id}: {a.decision.value}{target} (best {best}; capacity {a.evidence_capacity_bits:.1f})")
    return 0
