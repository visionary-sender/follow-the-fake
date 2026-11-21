"""`ftf link` and `ftf assign`: the linking engine on contract datasets."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from ftf.contract.models import Dataset, Relation
from ftf.linking.assign import combine, link_incident
from ftf.linking.grouping import ALL_RELATIONS, link_dataset


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
    parser.set_defaults(func=run_assign)


def _load(path: Path) -> Dataset:
    return Dataset.from_json(path.read_bytes())


def _write(ds: Dataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ds.to_json(), encoding="utf-8")


def run_link(args: argparse.Namespace) -> int:
    ds = _load(args.input)
    relations = [Relation(r) for r in args.relation] if args.relation else list(ALL_RELATIONS)
    out = link_dataset(ds, relations)
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
    results = [link_incident(base, inc, relation) for inc in new.incidents]
    out = combine(base, list(new.incidents))
    data = out.model_dump(mode="json")
    data["links"] += [l.model_dump(mode="json") for r in results for l in r.links]
    data["assignments"] += [r.assignment.model_dump(mode="json") for r in results]
    out = Dataset.model_validate(data)
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
