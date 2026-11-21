"""Spot-check the HACK-10 platform extractor against the planning findings. Runs on the VM only.

    python -I scripts/spot_check_platform.py --dataset /data/derived/ingest/organiser/dataset.json --root /data/raw

The planning report (cross-case signals) says which cases share a tenant, pixel, gateway, tracker
instance or form endpoint, and which cases carry an identifier at all. This script encodes only
those case groups, never the identifier values, and prints matches and misses as case ids,
plus indicator counts per kind and extraction time per capture. Nothing it prints is an
identifier value, so its output can go into a PR.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from ftf.contract import Dataset
from ftf.indicators import platform

# (kind, nodes that must share at least one value). A node is a case ("p3"), or a case with a
# capture alias ("2/acr", "61/s1"); a bare case matches the union of its captures.
SHARED = [
    ("meta_pixel", ["p1", "p2", "p3", "p4", "p7"]),
    ("meta_pixel", ["1", "2/acr", "p15"]),
    ("capi_gateway", ["p1", "p2", "p3", "p4", "p7"]),
    ("aio_cdn_tenant", ["1", "2/acr", "p1", "p2", "p3", "p4", "p7", "p15"]),
    ("aio_cdn_tenant", ["48", "61/s1", "61/s2"]),
    ("aio_cdn_tenant", ["p6", "p12"]),
    ("aio_landing", ["1", "2/acr", "p1", "p2", "p7", "p15"]),
    ("aio_flow", ["48", "61"]),
    ("aio_lander_template", ["48", "61"]),
    ("xid_tracker_machine", ["2/hl", "p5"]),
    ("tracker_postback", ["2/hl", "p5"]),
    ("form_endpoint", ["p9", "p10"]),
    ("google_ads_id", ["61/s1", "61/s2"]),
]
# (kind, nodes that must not share any value)
DISJOINT = [
    ("meta_pixel", ["1", "p1"]),
    ("capi_gateway", ["p1", "p8"]),
    ("aio_cdn_tenant", ["p1", "p11"]),
    ("aio_cdn_tenant", ["p1", "48"]),
    ("xid_tracker_machine", ["p5", "14"]),
]
# (kind, nodes that must each carry at least one value)
PRESENT = [
    ("meta_pixel", ["1", "10", "14", "36", "48", "p1", "p3", "p4", "p6", "p7", "p8", "p9", "p10", "p12", "p14", "p15"]),
    ("aio_owner", ["1", "48", "61", "p1", "p15"]),
    ("capi_gateway", ["p8", "p10"]),
    ("xid_tracker_machine", ["14"]),
    ("google_analytics_id", ["p14"]),
    ("clarity_id", ["p14"]),
    ("video_cdn_tenant", ["47", "p9", "p12"]),
    ("contact_email", ["36"]),
    ("form_field_set", ["p9", "p10", "p13"]),
]

ALIASES = {"acrosscommunities": "acr", "healthlinkq": "hl", "strona_1": "s1", "strona_2": "s2", "strona_3": "s3"}


def case_of(incident) -> str:
    ref = incident.source_ref or incident.id
    return ref.rsplit(":", 1)[-1].rsplit("-", 1)[-1]


def nodes_of(case: str, capture_id: str) -> set[str]:
    out = {case}
    for needle, alias in ALIASES.items():
        if needle in capture_id:
            out.add(f"{case}/{alias}")
    for alias in ("s1", "s2", "s3"):
        if capture_id.endswith("/" + alias):
            out.add(f"{case}/{alias}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, help="contract Dataset JSON (fixture or ftf ingest output)")
    ap.add_argument("--root", default="/data/raw", help="directory artefact paths are relative to")
    ap.add_argument("--json", help="also write the summary (no identifier values) to this file")
    args = ap.parse_args(argv)

    ds = Dataset.model_validate_json(Path(args.dataset).read_bytes())
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
                for node in nodes_of(case, cap.id):
                    values[(node, ind.kind)].add(ind.value)
    # p2 is collapsed onto p1 by ingest: let p2 stand for its canonical capture.
    for inc in ds.incidents:
        for cap in inc.captures:
            if cap.duplicate_of:
                src = next((i for i in ds.incidents for c in i.captures if c.id == cap.duplicate_of), None)
                if src is not None:
                    for (node, kind), vals in list(values.items()):
                        if node in nodes_of(case_of(src), cap.duplicate_of) and node == case_of(src):
                            values[(case_of(inc), kind)] |= vals

    known = {node for node, _ in values} | {case_of(i) for i in ds.incidents}
    checks = []

    def record(kind: str, test: str, nodes: list[str], ok: bool, missing: list[str]) -> None:
        checks.append({"test": test, "kind": kind, "nodes": nodes, "ok": ok, "absent_nodes": missing})

    for kind, nodes in SHARED:
        present = [n for n in nodes if n in known]
        sets = [values.get((n, kind), set()) for n in present]
        common = set.intersection(*sets) if sets else set()
        record(kind, "shared", nodes, bool(common) and len(present) == len(nodes), [n for n in nodes if not values.get((n, kind))])
    for kind, nodes in DISJOINT:
        sets = [values.get((n, kind), set()) for n in nodes]
        both = all(sets)
        record(kind, "disjoint", nodes, both and not set.intersection(*sets), [n for n, s in zip(nodes, sets) if not s])
    for kind, nodes in PRESENT:
        missing = [n for n in nodes if not values.get((n, kind))]
        record(kind, "present", nodes, not missing, missing)

    ok = sum(c["ok"] for c in checks)
    print(f"checks: {ok}/{len(checks)} match the planning findings")
    for c in checks:
        mark = "MATCH" if c["ok"] else "MISS "
        extra = f"  (no value in: {', '.join(c['absent_nodes'])})" if c["absent_nodes"] else ""
        print(f"  {mark} {c['test']:<8} {c['kind']:<22} {', '.join(c['nodes'])}{extra}")
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
            "checks": checks, "matched": ok, "total": len(checks),
            "seconds_per_capture": {"mean": statistics.mean(timings) if timings else None, "max": max(timings, default=None), "n": len(timings)},
            "per_kind": per_kind, "per_kind_live": per_kind_live,
        }, indent=2))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
