#!/usr/bin/env python3
"""Timings of the linking engine for docs/scaling.md (run on the workbench VM, never locally).

    python scripts/bench_linking.py --organiser DATASET.json --synthetic 1000 10000 --out DIR

For a real dataset: `ftf link` (all relations), index build, single-incident assignment with the
index vs the linear scan (the last few incidents held out), and the fragile-link counts (E6).
For the synthetic scale-up: the same on corpora of about N captures made of independent operator
blocks with fresh invented values (tests/linking/synthetic_corpus.py). Prints and writes only
timings and counts (no indicator values), as JSON to DIR/bench.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "linking"))

import synthetic_corpus as sc  # noqa: E402

from ftf.contract.models import Dataset, Relation  # noqa: E402
from ftf.linking import CandidateIndex, fragile_links, link_dataset, link_incident  # noqa: E402
from ftf.linking.sensitivity import summary  # noqa: E402


def _t(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def _split(ds: Dataset, hold: int) -> tuple[Dataset, list]:
    """Base = all but the last `hold` incidents whose duplicates point into the base."""
    incs = list(ds.incidents)
    base, new = incs[: len(incs) - hold], []
    known = {c.id for i in base for c in i.captures}
    for i in incs[len(incs) - hold :]:
        if all(c.duplicate_of is None or c.duplicate_of in known for c in i.captures):
            new.append(i)
        else:
            base.append(i)
            known |= {c.id for c in i.captures}
    return Dataset(name=ds.name, incidents=base), new


def link_blockwise(ds: Dataset) -> Dataset:
    """Campaign partition linked one synthetic operator block at a time (blocks share no values but
    a 0-bit platform constant), so the index and assignment can be timed at sizes where a full
    `ftf link` is too slow. Links are dropped; campaigns get unique ids."""
    blocks: dict[str, list] = {}
    for inc in ds.incidents:
        blocks.setdefault(inc.id.split("-", 1)[0], []).append(inc)
    camps = []
    for blk in blocks.values():
        for c in link_dataset(Dataset(name="blk", incidents=blk), [Relation.campaign]).campaigns:
            camps.append(c.model_copy(update={"id": f"campaign:campaign:{len(camps) + 1:05d}", "link_ids": []}))
    return ds.model_copy(update={"campaigns": camps, "links": [], "assignments": []})


def bench(ds: Dataset, label: str, hold: int, linear_max: int, sensitivity: bool, blockwise: bool = False) -> dict:
    n_caps = sum(len(i.captures) for i in ds.incidents)
    base_ds, new = _split(ds, hold)
    linked, t_link = _t(lambda: link_blockwise(base_ds) if blockwise else link_dataset(base_ds))
    idx, t_index = _t(lambda: CandidateIndex.build(linked, cache=False))
    t_idx, t_lin, same = [], [], 0
    for k, inc in enumerate(new):
        fast, t = _t(lambda: link_incident(linked, inc, Relation.campaign, index=idx))
        t_idx.append(t)
        if k < linear_max:
            lin, t = _t(lambda: link_incident(linked, inc, Relation.campaign))
            t_lin.append(t)
            same += fast.assignment == lin.assignment and fast.links == lin.links
    row = {
        "label": label,
        "link_mode": "blockwise campaign only" if blockwise else "ftf link, all relations",
        "incidents": len(ds.incidents),
        "captures": n_caps,
        "base_captures": sum(len(i.captures) for i in base_ds.incidents),
        "links": len(linked.links),
        "campaigns_campaign_relation": sum(1 for c in linked.campaigns if c.relation is Relation.campaign),
        "informative_values": len(idx.postings),
        "link_s": round(t_link, 3),
        "index_build_s": round(t_index, 3),
        "assign_index_ms_median": round(1000 * statistics.median(t_idx), 2) if t_idx else None,
        "assign_linear_ms_median": round(1000 * statistics.median(t_lin), 2) if t_lin else None,
        "assigned_incidents": len(t_idx),
        "linear_compared": len(t_lin),
        "linear_equal": same,
    }
    if sensitivity:
        recs, t_sens = _t(lambda: fragile_links(linked))
        row["sensitivity_s"] = round(t_sens, 3)
        row["e6"] = {r: {k: v for k, v in s.items() if k != "by_move"} for r, s in summary(recs)["relations"].items()}
        row["e6_by_layer_move"] = {
            r: {k.split(" -> ")[0]: v for k, v in s["by_move"].items()} for r, s in summary(recs)["relations"].items()
        }
    print(json.dumps(row), flush=True)
    return row


def synthetic(n_captures: int) -> Dataset:
    # about 2.1 captures per synthetic incident (ad + lander, some duplicates)
    n = max(int(n_captures / 2.1), 20)
    return sc.replicate(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--organiser", type=Path)
    ap.add_argument("--synthetic", type=int, nargs="*", default=[])
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--linear-max", type=int, default=10)
    ap.add_argument("--blockwise", action="store_true", help="synthetic: link block by block (see link_blockwise)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = []
    if a.organiser:
        ds = Dataset.from_json(a.organiser.read_bytes())
        rows.append(bench(ds, "organiser", hold=min(5, len(ds.incidents) // 5), linear_max=a.linear_max, sensitivity=True))
    for n in a.synthetic:
        rows.append(bench(synthetic(n), f"synthetic-{n}", hold=a.hold, linear_max=a.linear_max, sensitivity=n <= 2000 and not a.blockwise, blockwise=a.blockwise))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "bench.json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
