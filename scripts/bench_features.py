#!/usr/bin/env python3
"""Time `ftf features` (all families) per input on the current checkout (HACK-89).

Ranks the incidents of a dataset by how many OCR-able images they hold (ad screenshots and
screenshots of non-duplicate captures), writes the top --k as one-incident datasets under --out,
runs `ftf features` on each and records the wall-clock seconds. Only ranks, image counts, seconds
and exit codes are printed or stored: incident ids, paths and file names stay out of the output.

Runs on the visual VM only (never on the laptop):
  scripts/vm.sh run -- bash -c 'cd /work/<slug>/src && taskset -c 0,1 python3 -I scripts/bench_features.py \
      --dataset DATASET --raw-root RAW --out OUT --k 3 --label NAME'
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

IMAGE_TYPES = frozenset({"ad_screenshot", "screenshot"})


def image_count(inc) -> int:
    return sum(
        1
        for cap in inc.captures
        if not cap.duplicate_of
        for art in cap.artefacts
        if art.type.value in IMAGE_TYPES and art.path
    )


def main(argv=None) -> int:
    from ftf.features.common import read_dataset, write_dataset

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", required=True, type=Path)
    p.add_argument("--raw-root", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--label", default="run")
    a = p.parse_args(argv)

    ds = read_dataset(a.dataset)
    counts = [image_count(inc) for inc in ds.incidents]
    ranked = sorted(range(len(counts)), key=lambda i: -counts[i])[: a.k]
    a.out.mkdir(parents=True, exist_ok=True)
    records = []
    for rank, idx in enumerate(ranked):
        one = ds.model_copy(update={"incidents": [ds.incidents[idx]]})
        src = write_dataset(one, a.out / f"in-{rank}", "dataset.json")
        t0 = time.monotonic()
        r = subprocess.run(
            ["ftf", "features", "--dataset", str(src), "--raw-root", str(a.raw_root),
             "--out", str(a.out / f"run-{rank}")],
            capture_output=True, text=True,
        )
        secs = round(time.monotonic() - t0, 1)
        rec = {"label": a.label, "rank": rank, "incident_index": idx, "images": counts[idx],
               "seconds": secs, "returncode": r.returncode,
               "ocr_warnings": sum("OCR" in ln for ln in r.stderr.splitlines())}
        records.append(rec)
        print(json.dumps(rec), flush=True)
    (a.out / f"{a.label}.json").write_text(json.dumps(records, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
