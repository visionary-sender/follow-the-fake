"""`ftf enrich-domain`: keyless registry, DNS and certificate facts for domains, as contract Indicators.

The domains come from `ftf.indicators.domains.registered_domains`: from a contract
Dataset (`--from-dataset`), or from a folder run through ingest first (`--from-raw`, a thin
wrapper). Shared platforms, CDNs and SDK hosts are left out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ftf.contract import Dataset
from ftf.enrich.cache import Cache
from ftf.enrich.domain import SOURCES, DomainResult, Enricher, build_dataset
from ftf.enrich.normalise import DEFAULT_TLD_CACHE
from ftf.indicators import domains as domain_indicators
from ftf.ingest import run as ingest_run

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
        "--from-dataset",
        type=Path,
        metavar="PATH",
        help="take the registered domains of a contract Dataset JSON (shared platforms and CDNs left out)",
    )
    parser.add_argument(
        "--from-raw",
        type=Path,
        metavar="DIR",
        help="ingest this folder (organiser data, saved pages, HARs; runs every extractor, so slower), skip unreadable inputs with a warning, and take its registered domains as --from-dataset does",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="cache file (default: %(default)s)")
    parser.add_argument("--offline", action="store_true", help="never use the network; serve the cache (also FTF_OFFLINE=1)")
    parser.add_argument("--captured-on", type=date.fromisoformat, metavar="YYYY-MM-DD", help="capture date for domain_age_days_at_capture (default: lookup date)")
    parser.add_argument("--tld-cache", type=Path, default=DEFAULT_TLD_CACHE, help="tldextract cache directory (snapshot only, no download)")
    parser.add_argument("--out", type=Path, help="write the enrichment as a contract Dataset (JSON) here")
    parser.add_argument("--summary", type=Path, help="write the run summary (JSON) here instead of printing it")
    parser.set_defaults(func=run)


def _dataset_domains(dataset: Dataset, source: str) -> tuple[list[str], dict[str, Any]]:
    domains = domain_indicators.registered_domains(dataset, include_shared=False)
    dropped = domain_indicators.shared_dropped(dataset)
    info = {
        "source": source,
        "domains": len(domains),
        "shared_dropped": {"count": sum(dropped.values()), "by_category": dropped},
    }
    return domains, info


def _ingest_raw(root: Path) -> tuple[Dataset, list[str]]:
    """Ingest a folder as one input. If ingest rejects it, ingest each entry alone and skip the
    unreadable ones with a warning, as the old folder walk did. Returns the dataset and the names
    of the skipped entries."""
    try:
        return ingest_run([root], root=root).dataset, []
    except (ValueError, OSError) as exc:
        print(f"ftf enrich-domain: WARNING: ingest of {root} as one input failed ({exc}); reading its entries one by one", file=sys.stderr)
    incidents = []
    skipped: list[str] = []
    for entry in sorted(root.iterdir()):
        try:
            incidents.extend(ingest_run([entry], root=root).dataset.incidents)
        except (ValueError, OSError) as exc:
            skipped.append(entry.name)
            print(f"ftf enrich-domain: WARNING: skipped unreadable input {entry.name}: {exc}", file=sys.stderr)
    return Dataset(name=f"raw {root.name}", incidents=incidents), skipped


def _collect(args: argparse.Namespace) -> tuple[list[str], list[dict[str, Any]]]:
    raw: list[str] = list(args.domains)
    scopes: list[dict[str, Any]] = []
    if args.domains_file:
        for line in args.domains_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                raw.append(line.strip())
    if args.from_dataset:
        try:
            dataset = Dataset.from_json(args.from_dataset.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"--from-dataset is not a readable contract Dataset: {args.from_dataset}: {exc}") from exc
        domains, info = _dataset_domains(dataset, f"dataset {args.from_dataset}")
        raw.extend(domains)
        scopes.append(info)
    if args.from_raw:
        if not args.from_raw.is_dir():
            raise ValueError(f"--from-raw is not a directory: {args.from_raw}")
        dataset, skipped = _ingest_raw(args.from_raw)
        domains, info = _dataset_domains(dataset, f"raw {args.from_raw}")
        info["skipped_inputs"] = skipped
        raw.extend(domains)
        scopes.append(info)
    return list(dict.fromkeys(raw)), scopes  # keep first occurrence order, drop repeats


def _summary(results: list[DomainResult], invalid: list[dict[str, str]], enricher: Enricher, args: argparse.Namespace, scopes: list[dict[str, Any]]) -> dict[str, Any]:
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
        "scopes": scopes,
        "domains_enriched": len(results),
        "invalid_inputs": invalid,
        "sources": {src: {k: per_source[src].get(k, 0) for k in (*ORIGINS, "unavailable")} for src in SOURCES},
        "requests_per_host": dict(enricher.client.requests),
        "indicators": dict(sorted(kinds.items())),
        "domain_details": details,
    }


def run(args: argparse.Namespace) -> int:
    try:
        domains, scopes = _collect(args)
    except ValueError as exc:
        print(f"ftf enrich-domain: {exc}", file=sys.stderr)
        return 2
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
    seen: set[str] = set()
    for raw in domains:
        name = enricher.registrable(raw)
        if name is not None and name in seen:  # checked before any lookup, so no duplicate requests
            invalid.append({"input": raw, "error": f"duplicate of {name}; already enriched"})
            continue
        try:
            result = enricher.enrich(raw, captured_on=args.captured_on)
        except ValueError as exc:
            invalid.append({"input": raw, "error": str(exc)})
            continue
        seen.add(result.domain)
        results.append(result)

    if args.out:
        dataset = build_dataset(results, description=f"{len(results)} domains, keyless lookups; see README for sources and limits")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(dataset.to_json() + "\n", encoding="utf-8")

    text = json.dumps(_summary(results, invalid, enricher, args, scopes), indent=2, ensure_ascii=False)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0
