#!/usr/bin/env python3
"""Robustness pass over `ftf analyze`.

Runs `ftf analyze` offline, one subprocess per input with a timeout, over:
  - every organiser case folder (raw/do_*/*),
  - every BACKUP item (raw/BACKUP/*),
  - every URL line of raw/scam_links_odszyfrowane.txt (passed as an argument, never fetched),
  - a fixed-seed sample of corpus captures (folders holding url.txt under corpus/batch-*).

Writes, under --out: inputs.json (input id -> source path, private), results.jsonl (one record
per input), summary.json (aggregates per input group) and datasets/ and runs/ (analyze output).
Records carry no URLs, tokens or input strings: a URL is shown as <url>, the input as <input>.

Runs on the workbench VM only (scripts/vm.sh run -- python3 -I scripts/robustness_analyze.py ...).
Never run it on the laptop; never pass --online or --fetch.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SECRET_RE = re.compile(r"((?:access_token|token|secret|password|api_key|apikey)=)[^&\s'\"]+", re.IGNORECASE)
TRACE_LINES = 12
NOTE_CHARS = 200


@dataclass(frozen=True)
class Item:
    id: str
    group: str
    arg: str
    kind: str | None  # explicit analyze --kind, or None to let analyze guess
    name: str  # folder or item name for organiser inputs; empty for URLs and corpus


def organiser_items(raw: Path) -> list[Item]:
    items = []
    cases = sorted(p for p in raw.glob("do_*/*") if p.is_dir())
    items += [Item("", "case_folder", str(p), None, p.name) for p in cases]
    backup = sorted(raw.glob("BACKUP/*"))
    items += [Item("", "backup_item", str(p), None, p.name) for p in backup]
    urls = []
    for line in (raw / "scam_links_odszyfrowane.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if re.match(r"https?://", line, re.IGNORECASE):
            urls.append(line)
    items += [Item("", "bare_url", u, "url", "") for u in urls]
    return items


def corpus_items(corpus: Path, sample: int, seed: int) -> list[Item]:
    captures = sorted({p.parent for p in corpus.glob("batch-*/**/url.txt")})
    chosen = random.Random(seed).sample(captures, min(sample, len(captures)))
    return [Item("", "corpus_page", str(p), None, "") for p in chosen]


def number(items: list[Item], group_limit: int | None) -> list[Item]:
    counters: collections.Counter = collections.Counter()
    out = []
    for it in items:
        counters[it.group] += 1
        if group_limit is not None and counters[it.group] > group_limit:
            continue
        out.append(Item(f"{it.group}-{counters[it.group]:03d}", it.group, it.arg, it.kind, it.name))
    return out


def redact(text: str, arg: str) -> str:
    if arg:
        text = text.replace(arg, "<input>")
    text = SECRET_RE.sub(r"\g<1><redacted>", text)
    return URL_RE.sub("<url>", text)


def traceback_head(stderr: str, arg: str) -> tuple[list[str], str]:
    lines = stderr.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("Traceback")), None)
    if start is None:
        return [], ""
    block = [redact(line, arg) for line in lines[start:] if line.strip()]
    last = block[-1][:NOTE_CHARS] if block else ""
    return block[:TRACE_LINES], last


def count_indicators(dataset_path: Path, incident_id: str) -> tuple[int, dict, dict]:
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    layers: collections.Counter = collections.Counter()
    kinds: collections.Counter = collections.Counter()
    total = 0
    for inc in data.get("incidents", []):
        if inc.get("id") != incident_id:
            continue
        for ind in inc.get("indicators", []):
            total += 1
            layers[ind.get("layer", "?")] += 1
            kinds[ind.get("kind", "?")] += 1
    return total, dict(layers), dict(kinds)


def run_one(item: Item, args: argparse.Namespace, ftf: str) -> dict:
    # Analyze output (datasets, run copies of the input) can hold URLs with tokens: it stays in
    # the VM scratch folder, which is never synced to the bucket. Only redacted records leave it.
    run_dir = args.scratch / "runs" / item.id
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset = args.scratch / "datasets" / f"{item.id}.json"
    dataset.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ftf, "analyze", item.arg, "--base", str(args.base), "--out", str(dataset)]
    if item.kind:
        cmd += ["--kind", item.kind]
    env = {**os.environ, "FTF_OFFLINE": "1", "FTF_RUN_DIR": str(run_dir)}
    rec: dict = {"id": item.id, "group": item.group, "exit": None, "timed_out": False,
                 "crash": False, "traceback": [], "exception": "", "stage_errors": [], "kind": None,
                 "decision": None, "candidates": 0, "links": 0, "indicators": 0, "layers": {}, "kinds": {},
                 "seconds": 0.0}
    start = time.monotonic()
    try:
        # A new session lets a timeout kill the whole group (tesseract, ffmpeg), not just ftf.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", env=env, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            rec.update(timed_out=True, crash=True, seconds=round(time.monotonic() - start, 2))
            return rec
    except OSError as exc:  # missing binary and the like: a crash record, not an aborted run
        rec.update(crash=True, exception=redact(f"{exc.__class__.__name__}: {exc}", item.arg)[:NOTE_CHARS])
        return rec
    rec["seconds"] = round(time.monotonic() - start, 2)
    rec["exit"] = proc.returncode
    tb, exc = traceback_head(stderr, item.arg)
    rec["traceback"], rec["exception"] = tb, exc
    rec["crash"] = bool(tb) or proc.returncode not in (0, 1, 2)
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError:
        rec["crash"] = True
        rec["exception"] = rec["exception"] or "no JSON summary on stdout"
        return rec
    rec["kind"] = summary.get("input", {}).get("kind")
    rec["decision"] = summary.get("decision")
    rec["candidates"] = len(summary.get("candidates", []))
    rec["links"] = summary.get("links", 0)
    rec["stage_errors"] = [{"stage": s["name"], "note": redact(str(s.get("note") or ""), item.arg)[:NOTE_CHARS]}
                           for s in summary.get("stages", []) if s.get("status") == "error"]
    if dataset.exists():
        total, layers, kinds = count_indicators(dataset, summary.get("incident_id", ""))
        rec.update(indicators=total, layers=layers, kinds=kinds)
    else:
        rec["indicators"] = summary.get("indicators", 0)
    return rec


def run_safe(item: Item, args: argparse.Namespace, ftf: str) -> dict:
    """One unexpected failure in a parser or a missing file is a crash record, never an aborted run."""
    try:
        return run_one(item, args, ftf)
    except Exception as exc:  # noqa: BLE001 - the driver must reach every input
        return {"id": item.id, "group": item.group, "exit": None, "timed_out": False, "crash": True,
                "traceback": [], "exception": redact(f"{exc.__class__.__name__}: {exc}", item.arg)[:NOTE_CHARS],
                "stage_errors": [], "kind": None, "decision": None, "candidates": 0, "links": 0,
                "indicators": 0, "layers": {}, "kinds": {}, "seconds": 0.0}


def aggregate(records: list[dict]) -> dict:
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        groups[r["group"]].append(r)
    out = {}
    for group, rs in sorted(groups.items()):
        inds = [r["indicators"] for r in rs]
        layers: collections.Counter = collections.Counter()
        kinds: collections.Counter = collections.Counter()
        for r in rs:
            layers.update(r["layers"])
            kinds.update(r["kinds"])
        out[group] = {
            "inputs": len(rs),
            "crashes": sum(r["crash"] for r in rs),
            "timeouts": sum(r["timed_out"] for r in rs),
            "exit_codes": dict(collections.Counter(str(r["exit"]) for r in rs)),
            "inputs_with_stage_error": sum(1 for r in rs if r["stage_errors"]),
            "stage_errors": dict(collections.Counter(e["stage"] for r in rs for e in r["stage_errors"])),
            "zero_indicator_inputs": sum(1 for r in rs if r["indicators"] == 0),
            "indicators_min": min(inds) if inds else None,
            "indicators_median": statistics.median(inds) if inds else None,
            "indicators_max": max(inds) if inds else None,
            "indicator_layers": dict(layers),
            "indicator_kinds": dict(kinds),
            "decisions": dict(collections.Counter(str(r["decision"]) for r in rs)),
            "inputs_with_candidates": sum(1 for r in rs if r["candidates"]),
            "links": sum(r["links"] for r in rs),
            "seconds_total": round(sum(r["seconds"] for r in rs), 1),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--raw", type=Path, default=Path("/data/raw"))
    ap.add_argument("--corpus", type=Path, default=Path("/data/derived/corpus"))
    ap.add_argument("--base", type=Path, default=Path("/data/derived/robustness/base/dataset.json"))
    ap.add_argument("--out", type=Path, required=True, help="results folder (synced to the bucket)")
    ap.add_argument("--scratch", type=Path, required=True, help="VM-only folder for analyze output, never synced")
    ap.add_argument("--sample", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261122)
    ap.add_argument("--timeout", type=int, default=180, help="seconds per input")
    ap.add_argument("--limit", type=int, default=None, help="at most N inputs per group (smoke runs)")
    ap.add_argument("--jobs", type=int, default=1, help="inputs analysed in parallel (4 vCPU workbench)")
    args = ap.parse_args()

    ftf = str(Path(sys.executable).parent / "ftf")
    items = organiser_items(args.raw) + corpus_items(args.corpus, args.sample, args.seed)
    items = number(items, args.limit)
    args.out.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    # id -> source, kept in scratch. URLs are hashed so no token-bearing URL is written even there.
    source = {it.id: it.arg if it.kind != "url" else "sha256:" + hashlib.sha256(it.arg.encode()).hexdigest()[:16]
              for it in items}
    (args.scratch / "inputs.json").write_text(json.dumps(source, ensure_ascii=False, indent=1), encoding="utf-8")

    records = []
    with (args.out / "results.jsonl").open("w", encoding="utf-8") as fh, \
            ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for rec in pool.map(lambda it: run_safe(it, args, ftf), items):  # keeps input order
            records.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"{rec['id']} exit={rec['exit']} crash={rec['crash']} ind={rec['indicators']} "
                  f"stage_err={len(rec['stage_errors'])} {rec['seconds']}s", flush=True)

    summary = aggregate(records)
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
