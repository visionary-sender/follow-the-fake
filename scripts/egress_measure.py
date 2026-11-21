#!/usr/bin/env python3
"""HACK-45 measurement: does Google Cloud egress rotation get past rate limits and cloaking?

Standard library only. Three steps, each on the machine the rules allow:

  lists   (workbench)  cloaked batch-0 landers -> URL lists, sharded per egress worker
      python3 -I scripts/egress_measure.py lists --manifest M --out DIR --shards 3
  probe   (egress worker, as user ftf)  run HACK-20's scanner once per keyword and source
      python3 -I scripts/egress_measure.py probe --scanner /opt/scan/venv/bin/ftf --out DIR
  report  (workbench)  join probes, egress captures and baselines -> report.json + report.md
      python3 -I scripts/egress_measure.py report --egress DIR --manifest M [--baseline DIR ...]

Only manifests and summaries are read (JSON written by our own tools). Captured pages are never
opened, parsed or rendered here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CLOAKED = ("bot_wall", "decoy", "empty")
# Google searches advertiser names and domains: HACK-20's three terms plus a few more of the kind.
ATC_KEYWORDS = ["invest", "trading", "crypto", "forex", "bitcoin", "broker", "inwestycje", "kryptowaluty"]
META_KEYWORDS = ["platforma inwestycyjna", "pasywny dochód", "Brzoska", "Glapiński"]  # HACK-20's defaults
EGRESS_FILE = Path("/etc/ftf/egress.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def norm_url(url: str) -> str:
    """Requested URL without a fbclid parameter, for joining runs with and without --fbclid."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "fbclid"]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


# -- lists ------------------------------------------------------------------------------------


def cloaked_targets(rows: list[dict]) -> list[dict]:
    """Batch-0 rows whose fetch verdict was cloaked, one per URL, with whether Meta sourced it."""
    seen: dict[str, dict] = {}
    for r in rows:
        url = r.get("target_url")
        if r.get("fetch_outcome") not in CLOAKED or not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        key = norm_url(url)
        if key not in seen:
            seen[key] = {"url": url, "meta": r.get("source") == "meta", "source": r.get("source"),
                         "batch0_outcome": r.get("fetch_outcome"), "incident_id": r.get("incident_id")}
        elif r.get("source") == "meta":
            seen[key]["meta"] = True
    return list(seen.values())


def cmd_lists(args: argparse.Namespace) -> int:
    targets = cloaked_targets(_jsonl(args.manifest))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "targets.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in targets), encoding="utf-8")
    for kind in ("meta", "other"):
        group = [t["url"] for t in targets if t["meta"] == (kind == "meta")]
        for i in range(args.shards):
            shard = group[i :: args.shards]
            (args.out / f"landers-{kind}-{i + 1}.txt").write_text("".join(u + "\n" for u in shard), encoding="utf-8")
    c = Counter((t["source"], t["batch0_outcome"]) for t in targets)
    print(json.dumps({"targets": len(targets), "by_source_outcome": {f"{a}|{b}": n for (a, b), n in sorted(c.items())}}, indent=1))
    return 0


# -- probe ------------------------------------------------------------------------------------


def _egress() -> dict | None:
    try:
        return json.loads(EGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _last_json(text: str) -> dict | None:
    """The scanner prints its summary as one JSON line at the end of stdout."""
    for line in reversed(text.strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def probe_row(source: str, keyword: str, summary: dict | None, rc: int, seconds: float, stderr: str) -> dict:
    row = {"source": source, "keyword": keyword, "exit_code": rc, "seconds": round(seconds, 1)}
    src = ((summary or {}).get("sources") or {}).get(source) or {}
    queries = src.get("queries") or []
    q = next((x for x in queries if x.get("keyword") == keyword), queries[0] if queries else {})
    row["status"] = q.get("status") or (src.get("statuses") or ["no_summary"])[0]
    row["ads"] = int(src.get("collected") or 0)
    row["http_requests"] = (summary or {}).get("http_requests")
    if q.get("error"):
        row["error"] = str(q["error"])[:300]
    if row["status"] == "no_summary":
        row["error"] = stderr.strip()[-300:]
    return row


def cmd_probe(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    plan = [("google_atc", k) for k in args.atc_keywords] + [("meta", k) for k in args.meta_keywords]
    rows_path = args.out / "probe.jsonl"
    for i, (source, kw) in enumerate(plan):
        if i:
            time.sleep(args.pause)
        t0 = time.monotonic()
        cmd = [args.scanner, "scan", "--source", source, "--keywords", kw, "--out", str(args.out / f"scan-{source}"),
               "--max-ads", str(args.max_ads), "--max-per-keyword", str(args.max_ads), "--delay", str(args.delay)]
        if args.since:
            cmd += ["--since", args.since]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = -1, "", f"timeout after {args.timeout}s"
        summary = _last_json(out)
        if summary and isinstance(summary.get("run_id"), str):
            # The printed summary drops per-query detail; the run's summary.json keeps it.
            full = args.out / f"scan-{source}" / "runs" / summary["run_id"] / "summary.json"
            try:
                summary = json.loads(full.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        row = probe_row(source, kw, summary, rc, time.monotonic() - t0, err)
        row["at"] = _now()
        row["egress"] = _egress()
        with rows_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{row['at']} {source:<10} {kw!r:<26} {row['status']:<10} ads={row['ads']}", flush=True)
    return 0


# -- report -----------------------------------------------------------------------------------


def _captures(root: Path) -> list[dict]:
    out = []
    for m in sorted(root.glob("**/capture.json")):
        try:
            d = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "requested_url" in d and "outcome" in d:
            out.append(d)
    return out


def _by_url(caps: list[dict], profile: str | None = None) -> dict[str, dict]:
    """Latest capture per normalised requested URL (optionally only one visitor profile)."""
    out: dict[str, dict] = {}
    for c in sorted(caps, key=lambda c: c.get("started_at") or ""):
        if profile and (c.get("profile") or {}).get("name") != profile:
            continue
        out[norm_url(c["requested_url"])] = c
    return out


def summarise_probes(rows: list[dict]) -> dict:
    per_source: dict[str, dict] = {}
    per_egress: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows:
        s = per_source.setdefault(r["source"], {"queries": 0, "blocked": 0, "throttled": 0, "ok": 0, "empty": 0, "other": 0, "ads": 0})
        s["queries"] += 1
        st = r.get("status")
        s[st if st in ("blocked", "throttled", "ok", "empty") else "other"] += 1
        s["ads"] += int(r.get("ads") or 0)
        e = r.get("egress") or {}
        key = (r["source"], e.get("zone", "?"), e.get("ip_hash", "?")[:15])
        per_egress[key][st] += 1
        per_egress[key]["ads"] += int(r.get("ads") or 0)
    return {
        "per_source": per_source,
        "per_egress": [
            {"source": s, "zone": z, "ip": h, **dict(c)} for (s, z, h), c in sorted(per_egress.items())
        ],
    }


def summarise_landers(targets: list[dict], egress_caps: dict[str, dict], baseline: dict[str, dict]) -> dict:
    rows = []
    for t in targets:
        k = norm_url(t["url"])
        e, b = egress_caps.get(k), baseline.get(k)
        rows.append({
            "url_key": k, "source": t["source"], "meta": t["meta"], "batch0": t["batch0_outcome"],
            "sandbox_victim": b["outcome"] if b else None,
            "egress": e["outcome"] if e else None,
            "egress_zone": ((e or {}).get("egress") or {}).get("zone"),
        })
    done = [r for r in rows if r["egress"]]
    flipped = [r for r in done if r["egress"] == "live"]
    with_base = [r for r in done if r["sandbox_victim"]]
    by_source: dict[str, Counter] = defaultdict(Counter)
    for r in done:
        by_source[r["source"]][r["egress"]] += 1
    return {
        "targets": len(rows),
        "refetched": len(done),
        "egress_live": len(flipped),
        "egress_outcomes": dict(Counter(r["egress"] for r in done)),
        "egress_outcomes_by_source": {s: dict(c) for s, c in sorted(by_source.items())},
        "compared_with_sandbox_victim": len(with_base),
        "sandbox_victim_live": sum(r["sandbox_victim"] == "live" for r in with_base),
        "egress_live_on_compared": sum(r["egress"] == "live" for r in with_base),
        "only_egress_live": sum(r["egress"] == "live" and r["sandbox_victim"] != "live" for r in with_base),
        "only_sandbox_live": sum(r["sandbox_victim"] == "live" and r["egress"] != "live" for r in with_base),
        "rows": rows,
    }


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def render_md(probes: dict, landers: dict) -> str:
    lines = ["| Source | Queries | Blocked/throttled | OK (ads found) | Empty | Ads collected |", "|---|---|---|---|---|---|"]
    for s, v in sorted(probes["per_source"].items()):
        lines.append(f"| {s} | {v['queries']} | {v['blocked'] + v['throttled']} ({_pct(v['blocked'] + v['throttled'], v['queries'])}) "
                     f"| {v['ok']} | {v['empty']} | {v['ads']} |")
    lines += ["", "| Source | Zone | IP (salted hash) | Statuses | Ads |", "|---|---|---|---|---|"]
    for r in probes["per_egress"]:
        st = ", ".join(f"{k} {v}" for k, v in sorted(r.items()) if k not in ("source", "zone", "ip", "ads"))
        lines.append(f"| {r['source']} | {r['zone']} | `{r['ip']}` | {st} | {r.get('ads', 0)} |")
    L = landers
    lines += ["", f"Cloaked batch-0 landers: {L['targets']} URLs, {L['refetched']} re-fetched from Warsaw egress "
              f"(victim-pl-mobile; Meta items with l.facebook.com referer and fbclid).", "",
              "| | Live | Share |", "|---|---|---|",
              f"| Warsaw egress, all re-fetched | {L['egress_live']} / {L['refetched']} | {_pct(L['egress_live'], L['refetched'])} |",
              f"| Same URLs, egress (where the sandbox victim run exists) | {L['egress_live_on_compared']} / {L['compared_with_sandbox_victim']} | {_pct(L['egress_live_on_compared'], L['compared_with_sandbox_victim'])} |",
              f"| Same URLs, ftf-sandbox victim run (HACK-18) | {L['sandbox_victim_live']} / {L['compared_with_sandbox_victim']} | {_pct(L['sandbox_victim_live'], L['compared_with_sandbox_victim'])} |",
              "", "Egress outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(L["egress_outcomes"].items()))]
    for s, c in L["egress_outcomes_by_source"].items():
        lines.append(f"- {s}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    probe_rows = [r for p in sorted(args.egress.glob("**/probe.jsonl")) for r in _jsonl(p)]
    targets = cloaked_targets(_jsonl(args.manifest))
    egress_caps = _by_url([c for c in _captures(args.egress) if c.get("egress")])
    baseline = _by_url([c for d in args.baseline for c in _captures(d)], profile="victim-pl-mobile")
    probes = summarise_probes(probe_rows)
    landers = summarise_landers(targets, egress_caps, baseline)
    report = {"generated_at": _now(), "probes": probes, "landers": landers}
    (args.egress / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    md = render_md(probes, landers)
    (args.egress / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lists")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--shards", type=int, default=3)
    p.set_defaults(func=cmd_lists)
    p = sub.add_parser("probe")
    p.add_argument("--scanner", required=True, help="the `ftf` executable of a tree with ftf.scanner")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--atc-keywords", type=lambda s: [k for k in s.split(",") if k], default=ATC_KEYWORDS)
    p.add_argument("--meta-keywords", type=lambda s: [k for k in s.split(",") if k], default=META_KEYWORDS)
    p.add_argument("--max-ads", type=int, default=50)
    p.add_argument("--delay", type=float, default=5.0, help="seconds between requests within one scan")
    p.add_argument("--pause", type=float, default=20.0, help="seconds between two scans")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--since")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("report")
    p.add_argument("--egress", type=Path, required=True, help="the run directory pulled to the workbench")
    p.add_argument("--manifest", type=Path, required=True, help="HACK-18 batch-0 manifest.jsonl")
    p.add_argument("--baseline", type=Path, action="append", default=[], help="fetcher output dir of a sandbox victim run")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
