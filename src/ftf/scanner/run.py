"""`ftf scan`: one polite, capped pass over the ad libraries; new ads go to the queue."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from dataclasses import asdict

from ftf import egress
from ftf.contract import CONTRACT_VERSION, Incident
from ftf.scanner import certpl, creatives, google_atc, google_bq, meta
from ftf.scanner.http import Client
from ftf.scanner.records import EXTRACTOR, SOURCES, AdRecord, to_incident
from ftf.scanner.store import MANIFEST_VERSION, Locked, Store, update_dataset, write_capture

# Sources that search by keyword. They need terms from `--keywords` or `--keywords-file`; none are
# built in. The list in use is private (see docs/scanner.md), so nothing here names a term.
KEYWORD_SOURCES = ("meta", "google_atc")
CREATIVES_PER_AD = 2
CREATIVE_DELAY = 1.0  # image CDNs, not the libraries' search endpoints


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _keywords(value: str) -> list[str]:
    return [k.strip() for k in value.split(",") if k.strip()]


KeywordArg = Union[list[str], dict[str, list[str]], None]


def keywords_for(keywords: KeywordArg, source: str) -> list[str]:
    """Search terms of one source: a list applies to every keyword source, a dict is per source."""
    if not keywords or source not in KEYWORD_SOURCES:
        return []
    if isinstance(keywords, dict):
        return list(keywords.get(source, []))
    return list(keywords)


def load_keywords(path: Path) -> dict[str, list[str]]:
    """Read a keyword file: a `[source]` heading, then one search term per line; `#` lines are comments.

    Errors name the file and the line number only, never the terms.
    """
    out: dict[str, list[str]] = {}
    section = None
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in KEYWORD_SOURCES:
                raise ValueError(f"{path}:{number}: unknown heading; use {' or '.join(f'[{s}]' for s in KEYWORD_SOURCES)}")
            out.setdefault(section, [])
            continue
        if section is None:
            raise ValueError(f"{path}:{number}: a search term must come after a [source] heading")
        if line not in out[section]:
            out[section].append(line)
    return out


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 1: {value!r}")
    return n


def _non_negative(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0: {value!r}")
    return n


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "scan",
        help="search ad libraries for new ads and queue them for assignment",
        description=(
            "Search public ad libraries (Meta Ad Library via headless browser, Google Ads "
            "Transparency Center, CERT Polska warning list) by keyword and date, write one "
            "contract Incident per new ad and append it to the assignment queue. Landing pages "
            "are queued, never fetched."
        ),
    )
    p.add_argument("--source", action="append", choices=SOURCES, help="source to scan (repeatable; default: all)")
    keyword_src = p.add_mutually_exclusive_group()
    keyword_src.add_argument("--keywords", type=_keywords, help="comma-separated search terms for meta and google_atc")
    keyword_src.add_argument("--keywords-file", type=Path, help="keyword file with [meta] and [google_atc] headings, one term per line; required for meta and google_atc unless --keywords is given")
    p.add_argument("--cert-filter", type=_keywords, help="comma-separated substrings a CERT domain must contain (default: none)")
    p.add_argument("--since", type=_date, help="only ads shown / domains listed on or after this date (default: 7 days ago)")
    p.add_argument("--out", type=Path, default=Path("scan"), help="output directory with state and queue (default: ./scan)")
    p.add_argument("--max-ads", type=_positive, default=150, help="hard cap on new ads per source per run (default: 150)")
    p.add_argument("--max-per-keyword", type=_positive, default=50, help="cap on new ads per search term (default: 50)")
    p.add_argument("--delay", type=float, default=5.0, help="seconds between requests to one source (default: 5)")
    p.add_argument("--download-creatives", action="store_true", help="download creative images of new ads (sandbox only)")
    p.add_argument("--max-creatives", type=_non_negative, default=100, help="cap on creative images downloaded per run (default: 100)")
    p.add_argument("--bq-project", help="GCP project billed for the google_bq query (default: $FTF_BQ_PROJECT; without one google_bq is skipped)")
    p.set_defaults(func=cmd_scan)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"{_utc_stamp()} {msg}", file=sys.stderr, flush=True)


def scan(
    out: Path,
    sources: list[str],
    *,
    keywords: KeywordArg = None,
    cert_filter: Optional[list[str]] = None,
    since: Optional[date] = None,
    max_ads: int = 150,
    max_per_keyword: int = 50,
    delay: float = 5.0,
    download_creatives: bool = False,
    max_creatives: int = 100,
    bq_project: Optional[str] = None,
    now: Optional[datetime] = None,
    collectors: Optional[dict] = None,
) -> dict:
    """Run one scan and return its summary. `collectors` replaces the network sources in tests.

    Each source is saved (incidents, queue lines, seen state) as soon as it finishes, so a run
    stopped later (timeout, VM stop) keeps what the earlier sources found. Caps count only ads
    not seen before, so repeated runs go deeper instead of re-finding the top results.
    """
    now = now or datetime.now(timezone.utc)
    since = since or (now.date() - timedelta(days=7))
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    max_ads = max(1, max_ads)
    max_per_keyword = max(1, min(max_per_keyword, max_ads))
    collectors = collectors or {}
    queued_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    missing = [s for s in sources if s in KEYWORD_SOURCES and s not in collectors and not keywords_for(keywords, s)]
    if missing:  # checked before the store is touched
        raise ValueError(f"no search terms for {', '.join(missing)}: pass keywords or a keyword file")

    with Store(out) as store:
        seen = store.load_seen()
        client = Client(delay=delay)
        image_client = Client(delay=CREATIVE_DELAY)
        creative_budget = max(0, max_creatives) if download_creatives else 0
        egress_record = egress.current()
        upstream = egress.upstream_proxy()  # raises on a malformed proxy file: never silently go direct
        if upstream is not None:
            egress_record = {**(egress_record or {}), "proxy": upstream.label}
        summary: dict = {"run_id": run_id, "since": since.isoformat(), "sources": {}, "new": 0, "egress": egress_record}
        run_dir = store.run_dir(run_id)
        creative_errors: list[str] = []
        all_incidents: list[tuple[str, Incident]] = []

        for source in sources:
            t0 = time.monotonic()
            kws = keywords_for(keywords, source)
            skip = set(seen)
            if source in collectors:
                results = collectors[source](kws, since, max_ads, max_per_keyword, skip)
            elif source == "meta":
                results = meta.collect(kws, since=since.isoformat(), max_per_keyword=max_per_keyword, max_total=max_ads, delay=delay, skip=skip)
            elif source == "google_atc":
                results = google_atc.collect(kws, since=since, client=client, max_per_keyword=max_per_keyword, max_total=max_ads, skip=skip)
            elif source == "google_bq":
                results = [google_bq.collect(since=since, client=Client(delay=1.0), project=bq_project, max_total=max_ads, skip=skip)]
            else:
                results = [certpl.collect(cert_filter, since=since, client=client, max_total=max_ads, skip=skip)]
            unique: dict[str, AdRecord] = {}
            for res in results:
                for r in res.records:
                    if r.key not in seen:
                        unique.setdefault(r.key, r)
            new = list(unique.values())[:max_ads]

            incidents = []
            folders: list[str] = []
            for r in new:
                folder = r.folder(run_id)
                arts = []
                if creative_budget and r.creative_urls:
                    limit = min(CREATIVES_PER_AD, creative_budget)
                    arts, errs = creatives.download(r.creative_urls, out, folder, r.capture_ref, image_client, limit=limit)
                    creative_budget -= min(limit, len([u for u in r.creative_urls if creatives.allowed(u)]))
                    creative_errors.extend(errs)
                inc = to_incident(r, now.date(), arts)
                manifest = {
                    "manifest_version": MANIFEST_VERSION,
                    "contract_version": CONTRACT_VERSION,
                    "tool": EXTRACTOR,
                    "capture_id": folder,
                    "source": r.source,
                    "ad_id": r.ad_id,
                    "stable_id": r.incident_id,
                    "run_id": run_id,
                    "started_at": queued_at,
                    "finished_at": _utc_stamp(),
                    # Which egress worker / proxy sent the query (collector data; never the raw IP).
                    "egress": egress_record,
                    "incident": json.loads(inc.model_dump_json()),
                }
                ad = {**asdict(r), "key": r.key, "stable_id": r.incident_id, "ad_archive_id": None if r.source == "certpl" else r.ad_id,
                      "kind": "domain" if r.source == "certpl" else "ad", "landing_domain": r.effective_landing_domain(), "run_id": run_id}
                write_capture(out, folder, manifest, ad)
                incidents.append(inc)
                folders.append(folder)
                all_incidents.append((folder, inc))

            queued = 0
            if incidents:
                update_dataset(out, "scanner", incidents)
                queued = store.append_queue(
                    {
                        "key": r.key,
                        "source": r.source,
                        "ad_id": r.ad_id,
                        "incident_id": inc.id,
                        "capture_dir": f,
                        "landing_url": r.landing_url,
                        "landing_domain": r.effective_landing_domain(),
                        "keyword": r.keyword,
                        "run_id": run_id,
                        "queued_at": queued_at,
                        "status": "pending",
                    }
                    for r, inc, f in zip(new, incidents, folders)
                )
                for r in new:
                    seen[r.key] = run_id
                store.save_seen(seen)

            summary["new"] += queued
            summary["sources"][source] = {
                "collected": sum(len(res.records) for res in results),
                "new": queued,
                "seconds": round(time.monotonic() - t0, 1),
                "queries": [{"keyword": res.keyword, "status": res.status, "ads": len(res.records), **res.evidence} for res in results],
            }
            statuses = sorted({res.status for res in results})
            _log(f"{source}: {summary['sources'][source]['collected']} collected, {queued} new, status {'/'.join(statuses)}")
            # Keep a summary on disk after every source too.
            store.write_json(run_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

        summary["queue_length"] = len(store.read_queue())
        if creative_errors:
            summary["creative_errors"] = creative_errors[:50]
        summary["http_requests"] = client.requests + image_client.requests
        store.write_json(run_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        _log(f"run {run_id}: {summary['new']} new ads queued, queue length {summary['queue_length']}")
        summary["incidents"] = all_incidents  # not written to disk; for library callers
        return summary


def cmd_scan(args: argparse.Namespace) -> int:
    sources = list(dict.fromkeys(args.source or SOURCES))
    keywords: KeywordArg = args.keywords
    if args.keywords_file is not None:
        try:
            keywords = load_keywords(args.keywords_file)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"ftf scan: cannot read keyword file: {exc}", file=sys.stderr)
            return 2
    missing = [s for s in sources if s in KEYWORD_SOURCES and not keywords_for(keywords, s)]
    if missing:
        print(f"ftf scan: {', '.join(missing)} need search terms: pass --keywords or --keywords-file", file=sys.stderr)
        return 2
    try:
        summary = scan(
            args.out,
            sources,
            keywords=keywords,
            cert_filter=args.cert_filter,
            since=args.since,
            max_ads=args.max_ads,
            max_per_keyword=args.max_per_keyword,
            delay=args.delay,
            download_creatives=args.download_creatives,
            max_creatives=args.max_creatives,
            bq_project=args.bq_project,
        )
    except Locked as exc:
        _log(f"skipped: {exc}")
        return 75
    short = {k: v for k, v in summary.items() if k != "incidents"}
    short["sources"] = {
        s: {"collected": d["collected"], "new": d["new"], "statuses": sorted({q["status"] for q in d["queries"]})}
        for s, d in summary["sources"].items()
    }
    print(json.dumps(short, ensure_ascii=False))
    # Exit 0 when at least one source answered; a fully failed run is an error for the timer.
    ok = any(q["status"] in ("ok", "empty", "throttled") for d in summary["sources"].values() for q in d["queries"])
    return 0 if ok or not summary["sources"] else 1
