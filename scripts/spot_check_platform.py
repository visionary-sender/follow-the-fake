"""Spot-check the platform extractor against the planning findings. Runs on the VM only.

    python -I scripts/spot_check_platform.py \
        --dataset /data/derived/ingest/organiser/dataset-captures-only.json --root /data/raw \
        --expect /data/derived/spot-check/platform-expectations.json

The expectations file is organiser-derived data, so it lives in the bucket
(gs://$FTF_BUCKET/outputs/spot-check/), never in git. Its shape:

    {"shared":   [["meta_pixel", ["p1", "p3"]], ...],   # nodes must share at least one value
     "disjoint": [["meta_pixel", ["1", "p1"]], ...],    # no two nodes may share a value
     "present":  [["meta_pixel", ["10", "p8"]], ...]}   # each node must carry a value

A node is a case folder ("p3", "61") meaning the union of its captures, or a full capture id.
The script prints node names, kinds and counts only, never identifier values, so its output
can go into a PR. It also reports extraction time per capture.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from ftf.contract import Dataset
from ftf.indicators import platform


def case_of(incident) -> str:
    ref = incident.source_ref or incident.id
    return ref.rsplit(":", 1)[-1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, help="contract Dataset JSON (fixture or ftf ingest output)")
    ap.add_argument("--root", default="/data/raw", help="directory artefact paths are relative to")
    ap.add_argument("--expect", required=True, help="expectations JSON (organiser-derived, from the bucket)")
    ap.add_argument("--json", help="also write the summary (no identifier values) to this file")
    args = ap.parse_args(argv)

    ds = Dataset.model_validate_json(Path(args.dataset).read_bytes())
    expect = json.loads(Path(args.expect).read_text(encoding="utf-8"))
    root = Path(args.root)
    values: dict[tuple[str, str], set[str]] = defaultdict(set)  # (node, kind) -> values, in memory only
    per_kind: Counter[str] = Counter()
    per_kind_live: Counter[str] = Counter()
    timings: list[float] = []
    for inc in ds.incidents:
        case = case_of(inc)
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            t0 = time.perf_counter()
            inds = list(platform.extract(inc, cap, root))
            timings.append(time.perf_counter() - t0)
            for ind in inds:
                per_kind[ind.kind] += 1
                per_kind_live[ind.kind] += ind.observed.value == "live"
                for node in (case, cap.id):
                    values[(node, ind.kind)].add(ind.value)
    # An exact duplicate stands for its canonical capture.
    for inc in ds.incidents:
        for cap in inc.captures:
            if cap.duplicate_of:
                for (node, kind), vals in list(values.items()):
                    if node == cap.duplicate_of:
                        values[(case_of(inc), kind)] |= vals
                        values[(cap.id, kind)] |= vals

    checks = []
    for kind, nodes in expect.get("shared", []):
        sets = [values.get((n, kind), set()) for n in nodes]
        ok = bool(sets) and all(sets) and bool(set.intersection(*sets))
        checks.append(("shared", kind, nodes, ok, [n for n, s in zip(nodes, sets) if not s]))
    for kind, nodes in expect.get("disjoint", []):
        sets = [values.get((n, kind), set()) for n in nodes]
        ok = len(sets) >= 2 and all(sets) and all(not (a & b) for a, b in itertools.combinations(sets, 2))
        checks.append(("disjoint", kind, nodes, ok, [n for n, s in zip(nodes, sets) if not s]))
    for kind, nodes in expect.get("present", []):
        missing = [n for n in nodes if not values.get((n, kind))]
        checks.append(("present", kind, nodes, not missing, missing))

    matched = sum(c[3] for c in checks)
    print(f"checks: {matched}/{len(checks)} match the planning findings")
    for test, kind, nodes, ok, absent in checks:
        extra = f"  (no value in: {', '.join(absent)})" if absent else ""
        print(f"  {'MATCH' if ok else 'MISS '} {test:<8} {kind:<22} {', '.join(nodes)}{extra}")
    if timings:
        print(f"captures: {len(timings)}; seconds per capture: mean {statistics.mean(timings):.3f}, "
              f"median {statistics.median(timings):.3f}, max {max(timings):.3f}, total {sum(timings):.1f}")
    else:
        print("captures: 0")
    print("indicators per kind (live):")
    for kind, n in sorted(per_kind.items()):
        print(f"  {kind:<22} {n:>4} ({per_kind_live[kind]})")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "checks": [dict(zip(("test", "kind", "nodes", "ok", "absent_nodes"), c)) for c in checks],
            "matched": matched, "total": len(checks),
            "seconds_per_capture": {"mean": statistics.mean(timings) if timings else None,
                                    "max": max(timings, default=None), "n": len(timings)},
            "per_kind": per_kind, "per_kind_live": per_kind_live,
        }, indent=2))
    return 0 if matched == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
