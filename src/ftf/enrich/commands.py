"""`ftf enrich-domain`: keyless registry, DNS and certificate facts for domains, as contract Indicators."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ftf.enrich.cache import Cache
from ftf.enrich.domain import SOURCES, DomainResult, Enricher, build_dataset
from ftf.enrich.normalise import DEFAULT_TLD_CACHE
from ftf.enrich.scope import organiser_domains

DEFAULT_CACHE = Path(os.environ.get("FTF_ENRICH_CACHE") or "data/enrichment/cache.json")
ORIGINS = ("live", "cache", "stale-cache", "none")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "enrich-domain",
        help="registration, DNS, certificate and CERT Polska facts for domains (keyless, cached)",
        description=(
            "Look up each registrable domain in RDAP, DNS over HTTPS, crt.sh and the CERT Polska "
            "warning list, and write contract Indicators. Answers are cached with their fetch "
            "time. Without network the cache is used, and with no cache the indicators are "
            "`unknown`. Exit status is 0 unless the arguments are wrong."
        ),
    )
    parser.add_argument("domains", nargs="*", metavar="DOMAIN", help="domain or URL to enrich")
    parser.add_argument("--domains-file", type=Path, help="text file with one domain or URL per line")
    parser.add_argument(
        "--from-raw",
        type=Path,
        metavar="DIR",
        help="take the domains the organiser's data names (HAR page URLs, scam link list), read as inert text",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="cache file (default: %(default)s)")
    parser.add_argument("--offline", action="store_true", help="never use the network; serve the cache (also FTF_OFFLINE=1)")
    parser.add_argument("--captured-on", type=date.fromisoformat, metavar="YYYY-MM-DD", help="capture date for domain_age_days_at_capture (default: lookup date)")
    parser.add_argument("--tld-cache", type=Path, default=DEFAULT_TLD_CACHE, help="tldextract cache directory (snapshot only, no download)")
    parser.add_argument("--out", type=Path, help="write the enrichment as a contract Dataset (JSON) here")
    parser.add_argument("--summary", type=Path, help="write the run summary (JSON) here instead of printing it")
    parser.set_defaults(func=run)


def _collect(args: argparse.Namespace) -> tuple[list[str], dict[str, Any]]:
    raw: list[str] = list(args.domains)
    scope_info: dict[str, Any] = {}
    if args.domains_file:
        for line in args.domains_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                raw.append(line.strip())
    if args.from_raw:
        scope = organiser_domains(args.from_raw, tld_cache=args.tld_cache)
        raw.extend(scope.domains)
        scope_info = {
            "root": str(args.from_raw),
            "domains": len(scope.domains),
            "urls_by_origin": dict(scope.counts),
            "domains_by_origin": dict(Counter(o for origins in scope.sources.values() for o in origins)),
            "skipped_files": scope.skipped,
        }
    return list(dict.fromkeys(raw)), scope_info  # keep first occurrence order, drop repeats


def _summary(results: list[DomainResult], invalid: list[dict[str, str]], enricher: Enricher, args: argparse.Namespace, scope_info: dict[str, Any]) -> dict[str, Any]:
    per_source: dict[str, Counter[str]] = {src: Counter() for src in SOURCES}
    kinds: Counter[str] = Counter()
    details = []
    for res in results:
        lookups = {}
        for src, look in res.lookups.items():
            per_source[src][look.origin if look.origin != "none" else "unavailable"] += 1
            lookups[src] = {"status": look.status, "origin": look.origin, "fetched_at": look.fetched_at, "reason": look.reason}
        for ind in res.incident.indicators:
            kinds[ind.kind] += 1
        details.append({
            "domain": res.domain,
            "lookups": lookups,
            "indicators": len(res.incident.indicators),
            "unknown": sum(1 for ind in res.incident.indicators if ind.value == "unknown"),
        })
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "offline": enricher.offline,
        "cache": str(args.cache),
        "cache_entries": len(enricher.cache),
        "cache_warnings": enricher.cache.warnings,
        "captured_on": args.captured_on.isoformat() if args.captured_on else "lookup date (assumed)",
        "scope": scope_info or None,
        "domains_enriched": len(results),
        "invalid_inputs": invalid,
        "sources": {src: {k: per_source[src].get(k, 0) for k in (*ORIGINS, "unavailable")} for src in SOURCES},
        "requests_per_host": dict(enricher.client.requests),
        "indicators": dict(sorted(kinds.items())),
        "domain_details": details,
    }


def run(args: argparse.Namespace) -> int:
    domains, scope_info = _collect(args)
    if not domains:
        print("ftf enrich-domain: no domains given (see --help)", file=sys.stderr)
        return 2
    enricher = Enricher(
        Cache(args.cache),
        offline=args.offline or _env_flag("FTF_OFFLINE"),
        tld_cache=args.tld_cache,
    )
    results: list[DomainResult] = []
    invalid: list[dict[str, str]] = []
    for raw in domains:
        try:
            results.append(enricher.enrich(raw, captured_on=args.captured_on))
        except ValueError as exc:
            invalid.append({"input": raw, "error": str(exc)})

    if args.out:
        dataset = build_dataset(results, description=f"{len(results)} domains, keyless lookups; see README for sources and limits")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(dataset.to_json() + "\n", encoding="utf-8")

    text = json.dumps(_summary(results, invalid, enricher, args, scope_info), indent=2, ensure_ascii=False)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0
