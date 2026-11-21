"""CLI of the linking engine. `ftf score IN.json A B [--relation R]` prints one pair's ledger."""

from __future__ import annotations

import argparse
from pathlib import Path

from ftf.contract.models import Dataset, Relation
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
    parser.set_defaults(func=run_score)


def run_score(args: argparse.Namespace) -> int:
    ds = Dataset.from_json(args.dataset.read_bytes())
    ctx = ScoringContext.from_dataset(ds)
    score = score_pair(ctx, args.a, args.b, Relation(args.relation))
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
