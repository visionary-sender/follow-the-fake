"""Evaluate the kit fingerprints (HACK-11) on a contract dataset; runs on the workbench VM.

    python -I scripts/eval_kit.py DATASET_JSON --root /data/raw --out /data/derived/kit-eval/<name> \
        [--aliases ALIASES_JSON]

Runs only the `kit` extractor (through `extract_capture`, so the stop-list and token redaction
apply) on every non-duplicate capture, times each capture, and compares capture pairs:

- known pairs from the cross-case signal report (04 §b/§c): same kit, or unrelated;
- every pair of nodes, to list any unexpected sharing.

A "node" is a case (`1`, `p15`; all its captures merged) or, for the folders that hold several
sites, one capture (`61/s1`, `2/<capture>`). Capture names in those folders can be real
domains, so the labels used by the known-pair list (`2/acr`, `2/hl`) come from --aliases, a JSON
object {capture id: node label} kept in the data bucket
(gs://follow-the-fake-data/outputs/kit-eval/aliases.json), never in git. Values shared by at least --platform-df nodes are counted separately as
platform-wide (the report's ">= 8 cases" rule). Writes `summary.json` and `pairs.md` (kinds and
counts only, no values) and `indicators.json` (the extracted indicators) to --out.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from ftf.contract import Dataset
from ftf.indicators import extract_capture, kit

# Pairs from plans/knowledge/04-cross-case-signals.md: strict components and contradicted links.
SAME_KIT = [
    ("1", "p15"), ("1", "p7"), ("p1", "p7"), ("p15", "p7"), ("1", "2/acr"),
    ("p3", "p4"), ("48", "61/s1"), ("48", "61/s2"), ("2/hl", "3"), ("3", "p5"),
    ("p6", "p12"), ("p8", "p11"), ("p9", "p10"),
]
UNRELATED = [
    ("1", "48"), ("p15", "61/s1"), ("1", "3"), ("p3", "p6"), ("p8", "p9"), ("p5", "p12"),
    ("p10", "p11"), ("14", "p7"), ("36", "p4"), ("47", "p1"), ("10", "p13"), ("2/acr", "2/hl"),
]

_CASE = [
    re.compile(r"^(?:do-|do_udostepnienia:)(\d+)(?:[/:](.+))?$"),
    re.compile(r"^(?:backup-|BACKUP:)(p\d+)(?:[/:](.+))?$"),
]


def node_of(capture_id: str, multi: set[str], aliases: dict[str, str] | None = None) -> str | None:
    """Node label of a capture id from either id scheme (fixture `do-61/s1`, ingest `do_udostepnienia:61:strona_1`)."""
    if aliases and capture_id in aliases:
        return aliases[capture_id]
    for pat in _CASE:
        m = pat.match(capture_id)
        if m:
            case, rest = m.group(1), (m.group(2) or "")
            if case not in multi:
                return case
            rest = re.sub(r"^strona_(\d+)", r"s\1", rest) or "c"
            return f"{case}/{rest}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--root", type=Path, default=Path("/data/raw"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--platform-df", type=int, default=8)
    ap.add_argument("--aliases", type=Path, help="JSON {capture id: node label} (data bucket, not git)")
    args = ap.parse_args(argv)

    ds = Dataset.from_json(args.dataset.read_bytes())
    aliases = json.loads(args.aliases.read_text()) if args.aliases else {}
    multi = {"2", "61"}  # folders that hold several different sites (04 §Nodes)

    values: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))  # node -> kind -> values
    timings: list[float] = []
    per_capture = []
    all_inds = []
    for inc in ds.incidents:
        for cap in inc.captures:
            if cap.duplicate_of is not None or cap.stage.value == "ad":
                continue
            t0 = time.perf_counter()
            inds = extract_capture(inc, cap, args.root, [kit])
            dt = time.perf_counter() - t0
            timings.append(dt)
            node = node_of(cap.id, multi, aliases) or cap.id
            per_capture.append({"capture": cap.id, "node": node, "seconds": round(dt, 4), "indicators": len(inds)})
            for ind in inds:
                values[node][ind.kind].add(ind.value)
            all_inds.extend(ind.model_dump(mode="json") for ind in inds)

    df: dict[tuple[str, str], int] = defaultdict(int)
    for kinds in values.values():
        for kind, vals in kinds.items():
            for v in vals:
                df[(kind, v)] += 1

    def compare(a: str, b: str) -> dict:
        kinds: dict[str, int] = {}
        wide = 0
        for kind in sorted(set(values[a]) | set(values[b])):
            common = values[a].get(kind, set()) & values[b].get(kind, set())
            narrow = [v for v in common if df[(kind, v)] < args.platform_df]
            wide += len(common) - len(narrow)
            if narrow:
                kinds[kind] = len(narrow)
        return {"a": a, "b": b, "shared": kinds, "shared_total": sum(kinds.values()), "platform_wide": wide}

    present = set(values)
    known = []
    for label, pairs in (("same_kit", SAME_KIT), ("unrelated", UNRELATED)):
        for a, b in pairs:
            if a in present and b in present:
                known.append({"expected": label, **compare(a, b)})
    all_pairs = [compare(a, b) for a, b in itertools.combinations(sorted(present), 2)]
    sharing = [p for p in all_pairs if p["shared_total"]]

    summary = {
        "dataset": ds.name,
        "captures": len(per_capture),
        "nodes": sorted(present),
        "platform_df": args.platform_df,
        "seconds_per_capture": {
            "mean": round(statistics.mean(timings), 4) if timings else None,
            "median": round(statistics.median(timings), 4) if timings else None,
            "max": round(max(timings), 4) if timings else None,
            "total": round(sum(timings), 2),
        },
        "indicators_by_kind": {k: sum(1 for i in all_inds if i["kind"] == k) for k in kit.kinds()},
        "known_pairs": known,
        "sharing_pairs": sharing,
        "per_capture": per_capture,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    (args.out / "indicators.json").write_text(json.dumps(all_inds))
    (args.out / "pairs.md").write_text(_markdown(summary))
    print(_markdown(summary))
    return 0


def _markdown(s: dict) -> str:
    def row(p: dict, extra: str = "") -> str:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(p["shared"].items())) or "none"
        return f"| {extra}{p['a']} – {p['b']} | {p['shared_total']} | {p['platform_wide']} | {kinds} |"

    t = s["seconds_per_capture"]
    lines = [
        f"Dataset `{s['dataset']}`: {s['captures']} captures, {len(s['nodes'])} nodes; seconds per capture "
        f"mean {t['mean']}, median {t['median']}, max {t['max']}, total {t['total']}.",
        "",
        f"Known pairs (shared = values seen in fewer than {s['platform_df']} nodes; platform-wide counted apart):",
        "",
        "| Pair | Shared | Platform-wide | Shared kinds (count) |",
        "|---|---|---|---|",
    ]
    lines += [row(p, f"{p['expected']}: ") for p in s["known_pairs"]]
    known = {(p["a"], p["b"]) for p in s["known_pairs"]} | {(p["b"], p["a"]) for p in s["known_pairs"]}
    other = [p for p in s["sharing_pairs"] if (p["a"], p["b"]) not in known]
    lines += ["", "Other node pairs that share any value:", "", "| Pair | Shared | Platform-wide | Shared kinds (count) |", "|---|---|---|---|"]
    lines += [row(p) for p in other] or ["| none | | | |"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
