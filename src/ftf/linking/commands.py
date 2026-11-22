"""CLI of the linking engine.

`ftf score IN.json A B [--relation R]` prints one pair's ledger; `ftf link` writes one partition
per relation; `ftf index` builds the candidate index of a linked collection; `ftf assign` assigns
new incidents to it (through the index, no rescan); `ftf sensitivity` flags fragile links (±2 bits).
`--content-views CALIBRATION.json` (from `ftf calibrate-similarity`) switches on the calibrated
content views of `score`, `link` and `assign`; without it they are off.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ftf.contract.models import Dataset, Relation
from ftf.linking.assign import combine, link_incident
from ftf.linking.backend import Backend
from ftf.linking.grouping import ALL_RELATIONS, link_dataset
from ftf.linking.index import CandidateIndex
from ftf.linking.learn_commands import add_weights_argument, context_from_args, load_weights_arg
from ftf.linking.ledger import ScoringContext, level_of, score_pair, threshold, verdict, with_content
from ftf.linking.similarity import DatasetVectorStore, content_views, load_calibrations
from ftf.linking.sensitivity import annotate_assignment, annotate_dataset, assignment_flips, fragile_links, summary
from ftf.linking.types import PairScore
from ftf.linking.weights import HAND_TABLE, warn_unknown_kinds


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
    add_content_arguments(parser)
    parser.set_defaults(func=run_score)


def run_score(args: argparse.Namespace) -> int:
    try:
        ds = Dataset.from_json(args.dataset.read_bytes())
        ctx = with_content(context_from_args(ds, args), content_from_args(args, args.dataset))
        warn_unknown_kinds(ds, ctx.weights, "score")
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
    add_content_arguments(parser)
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
    parser.add_argument("--index", type=Path, metavar="INDEX.json", help="candidate index from `ftf index` (default: built in memory)")
    parser.add_argument("--linear", action="store_true", help="score every campaign instead of using the candidate index (same result, slower)")
    parser.add_argument("--sensitivity", action="store_true", help="note decisions that flip when one layer moves by --delta bits")
    parser.add_argument("--delta", type=float, default=2.0, help="bits for --sensitivity (default: %(default)s)")
    add_weights_argument(parser)
    add_content_arguments(parser)
    parser.add_argument("--new-vectors-dir", type=Path, action="append", metavar="DIR",
                        help="where NEW.json's vector stores live (repeatable; default: the directory of NEW.json)")
    parser.set_defaults(func=run_assign)


def _backend_factory(args: argparse.Namespace, prog: str):
    """A backend factory for `--weights` (file read once), None without it, or an int exit code on a bad file."""
    loaded = _weights(args, prog)
    if loaded is None or isinstance(loaded, int):
        return loaded
    return lambda ds: Backend.from_context(context_from_args(ds, args, loaded))


def _weights(args: argparse.Namespace, prog: str):
    """(table, background) from `--weights`, None without it, or an int exit code on a bad file."""
    if not args.weights:
        return None
    try:
        return load_weights_arg(args)
    except ValueError as exc:
        print(f"ftf {prog}: {exc}", file=sys.stderr)
        return 2


def _load(path: Path) -> Dataset:
    return Dataset.from_json(path.read_bytes())


def _write(ds: Dataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ds.to_json(), encoding="utf-8")


def run_link(args: argparse.Namespace) -> int:
    factory = _backend_factory(args, "link")
    if isinstance(factory, int):
        return factory
    ds = _load(args.input)
    relations = list(dict.fromkeys(Relation(r) for r in args.relation)) if args.relation else list(ALL_RELATIONS)
    try:
        content = content_from_args(args, args.input)
    except ValueError as exc:
        print(f"ftf link: {exc}", file=sys.stderr)
        return 2
    backend = factory(ds) if factory else None
    if content is not None:
        ctx = backend.context if backend is not None else ScoringContext.from_dataset(ds)
        backend = Backend.from_context(with_content(ctx, content))
    warn_unknown_kinds(ds, backend.context.weights if backend else HAND_TABLE, "link")
    out = link_dataset(ds, relations, backend=backend)
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
    if args.delta <= 0:
        print("ftf assign: --delta must be positive", file=sys.stderr)
        return 2
    loaded = _weights(args, "assign")
    if isinstance(loaded, int):
        return loaded
    table, background = loaded if loaded is not None else (HAND_TABLE, None)
    factory = (lambda ds: Backend.from_context(context_from_args(ds, args, loaded))) if loaded is not None else None
    base = _load(args.collection)
    new = _load(args.new)
    relation = Relation(args.relation)
    if not any(c.relation is relation for c in base.campaigns):
        print(f"ftf assign: {args.collection} has no {relation.value} campaigns; run `ftf link` with that relation first", file=sys.stderr)
        return 2
    if args.linear and args.index:
        print("ftf assign: --index and --linear exclude each other", file=sys.stderr)
        return 2
    try:
        calibrations = load_calibrations(args.content_views) if args.content_views else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ftf assign: --content-views: {exc}", file=sys.stderr)
        return 2
    store = None
    if calibrations:
        if args.sensitivity:
            print("ftf assign: --sensitivity does not support --content-views yet", file=sys.stderr)
            return 2
        new_dirs = args.new_vectors_dir or [args.new.parent]
        base_dirs = args.vectors_dir or [args.collection.parent]
        store = DatasetVectorStore(
            new_dirs[0], {c.id: base_dirs[0] for i in base.incidents for c in i.captures}, extra_dirs=[*base_dirs, *new_dirs]
        )
        if content_views(calibrations, store) is None:
            calibrations = None
    index: Optional[CandidateIndex] = None
    if not args.linear and not calibrations:  # content views score every campaign (assign.py)
        try:
            if args.index:
                index = CandidateIndex.load(args.index, base, weights=table, background=background)
            else:
                index = CandidateIndex.build(base, weights=table, background=background, cache=False)
        except (OSError, ValueError, KeyError) as exc:
            print(f"ftf assign: {exc}", file=sys.stderr)
            return 2
    # New incidents may duplicate each other's captures (`duplicate_of` points at the smallest id):
    # add them one at a time, each after the captures it points at, so each one validates.
    # With the index, each one is added to it (rarity, τ) and the collection is never rescanned;
    # the output dataset is validated once at the end.
    known = {c.id for i in base.incidents for c in i.captures}
    pending = list(new.incidents)
    results = []
    added: list = []
    current = base
    while pending:
        ready = [i for i in pending if all(c.duplicate_of is None or c.duplicate_of in known or c.duplicate_of in {x.id for x in i.captures} for c in i.captures)]
        if not ready:
            missing = sorted({c.duplicate_of for i in pending for c in i.captures if c.duplicate_of and c.duplicate_of not in known})
            print(f"ftf assign: duplicate_of points at unknown captures: {', '.join(missing)}", file=sys.stderr)
            return 2
        for inc in ready:
            # with the index, it carries the --weights options; without it, the backend factory does
            be = None if index is not None else factory
            try:
                res = link_incident(current, inc, relation, index=index, backend=be, calibrations=calibrations, vector_store=store)
            except ValueError as exc:
                print(f"ftf assign: {inc.id}: {exc}", file=sys.stderr)
                return 2
            if args.sensitivity:
                rec = assignment_flips(current, inc, res.assignment, relation, index=index, delta=args.delta, weights=table, backend=be)
                res.assignment = annotate_assignment(res.assignment, rec)
            results.append(res)
            if index is not None:
                index.add(inc)
            else:
                current = combine(current, [inc])
            added.append(inc)
            known |= {c.id for c in inc.captures}
            pending.remove(inc)
    data = combine(base, added).model_dump(mode="json") if index is not None else current.model_dump(mode="json")
    data["links"] += [l.model_dump(mode="json") for r in results for l in r.links]
    data["assignments"] += [r.assignment.model_dump(mode="json") for r in results]
    try:
        out = Dataset.model_validate(data)
    except ValidationError as exc:
        print(f"ftf assign: the result does not validate: {exc}", file=sys.stderr)
        return 2
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


def register_index(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "index",
        help="build the candidate index of a linked collection",
        description="Index a linked collection (the output of `ftf link`) for candidate search: informative "
        "value -> captures, capture -> campaign per relation, rarity counts. Written as JSON (no pickle).",
    )
    parser.add_argument("collection", type=Path, metavar="LINKED.json")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="INDEX.json")
    add_weights_argument(parser)
    parser.set_defaults(func=run_index)


def run_index(args: argparse.Namespace) -> int:
    loaded = _weights(args, "index")
    if isinstance(loaded, int):
        return loaded
    table, background = loaded if loaded is not None else (HAND_TABLE, None)
    try:
        base = _load(args.collection)
    except (OSError, ValidationError) as exc:
        print(f"ftf index: {exc}", file=sys.stderr)
        return 2
    idx = CandidateIndex.build(base, weights=table, background=background, cache=False)
    idx.save(args.out)
    n_camp = {r.value: len(c) for r, c in idx.campaigns.items() if c}
    print(f"{len(idx.incidents)} incidents, {idx.n_captures} canonical captures, {len(idx.postings)} informative values -> {args.out}")
    print("  campaigns: " + ", ".join(f"{k} {v}" for k, v in n_camp.items()))
    return 0


def register_sensitivity(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sensitivity",
        help="flag links whose verdict flips when one layer's weights move by ±2 bits",
        description="Recompute every ledger link of LINKED.json with each layer's weights moved by -DELTA and +DELTA "
        "(one layer at a time, then all together) and count the flipped verdicts per relation.",
    )
    parser.add_argument("collection", type=Path, metavar="LINKED.json")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="REPORT.json")
    parser.add_argument("--delta", type=float, default=2.0, help="bits to move each layer by (default: %(default)s)")
    parser.add_argument("--annotate", type=Path, metavar="OUT.json", help="also write LINKED.json with fragile notes on the ledger rows")
    parser.set_defaults(func=run_sensitivity)


def run_sensitivity(args: argparse.Namespace) -> int:
    if args.delta <= 0:
        print("ftf sensitivity: --delta must be positive", file=sys.stderr)
        return 2
    try:
        ds = _load(args.collection)
    except (OSError, ValidationError) as exc:
        print(f"ftf sensitivity: {exc}", file=sys.stderr)
        return 2
    records = fragile_links(ds, delta=args.delta)
    report = {**summary(records, args.delta), "links": records}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.annotate:
        _write(annotate_dataset(ds, records), args.annotate)
    print(f"{len(records)} links, ±{args.delta:g} bits per layer -> {args.out}")
    for rel, s in report["relations"].items():
        print(f"  {rel}: {s['fragile']} of {s['links']} fragile (single layer), {s['worst_case_flips']} flip if every layer moves")
    return 0


def add_content_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--content-views", type=Path, metavar="CALIBRATION.json",
        help="switch on the calibrated content views (text/image similarity) with these calibrations "
        "from `ftf calibrate-similarity` (default: off)",
    )
    parser.add_argument(
        "--vectors-dir", type=Path, action="append", metavar="DIR",
        help="where the dataset's vector stores (vectors/...) live (repeatable; default: the dataset's directory)",
    )


def content_from_args(args: argparse.Namespace, dataset_path: Path):
    """The `ContentViews` switch from `--content-views` / `--vectors-dir`, or None (off). ValueError on a bad file."""
    if not getattr(args, "content_views", None):
        return None
    try:
        calibrations = load_calibrations(args.content_views)
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError(f"--content-views: {exc}") from None
    dirs = args.vectors_dir or [dataset_path.parent]
    return content_views(calibrations, DatasetVectorStore(dirs[0], extra_dirs=dirs[1:]))
