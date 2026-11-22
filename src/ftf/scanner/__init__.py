"""Ad-library scanner: find new ads in public ad libraries and queue them for assignment.

Library use (the CLI `ftf scan` wraps the same code):

    from ftf import scanner
    for ad in scanner.scan(["certpl", "google_atc"], keywords=["invest"], since=date(2026, 11, 14), out=Path("scan")):
        ad.capture_dir, ad.ad   # stable: the capture folder and its ad.json fields
        ad.incident             # provisional contract Incident (also in capture.json)
    for item in scanner.pending(Path("scan")):
        ...  # assign item["incident_id"] (capture folder item["capture_dir"]) to a campaign
    scanner.mark(Path("scan"), [item["key"]], "assigned")

The stable key of an ad is its capture folder (`capture_id`) with `ad.json`. The Incident in
`capture.json` is provisional: Ingestion's adapter builds the canonical one from the folder.

`scan` returns only ads not seen in earlier runs against the same `out` directory. Without
`out` it scans into a fresh temporary directory, so nothing is deduplicated across calls.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Union

from ftf.contract import Incident
from ftf.scanner.records import SOURCES
from ftf.scanner.store import QUEUE_STATUSES, Locked, Store, load_incident, set_queue_status


@dataclass(frozen=True)
class ScannedAd:
    key: str  # "<source>:<ad id>", the deduplication key
    stable_id: str  # cross-project id: `adlib:<ad archive id>` or `certpl:<registrable domain>`
    source: str
    ad_id: str
    capture_id: str  # the folder name (`<run stamp>-<source>-<ad id>`)
    capture_dir: Path
    ad: dict  # the ad.json fields
    incident: Incident  # provisional; see the module docstring


def scan(
    sources: Optional[list[str]] = None,
    keywords: Optional[Union[list[str], dict[str, list[str]]]] = None,
    since: Optional[date] = None,
    *,
    out: Optional[Path] = None,
    **options,
) -> list[ScannedAd]:
    """Run one scan and return the new ads (capture folder, ad.json fields, provisional Incident).

    `keywords` is a list for every keyword source (meta, google_atc), or a dict per source, as read
    by `ftf.scanner.run.load_keywords`. There are no built-in terms: a keyword source without terms
    raises ValueError. `options` are the CLI's: cert_filter, max_ads, max_per_keyword, delay,
    download_creatives, max_creatives. Raises `Locked` if another scan holds `out`.
    """
    from ftf.scanner.run import scan as _scan

    for s in sources or ():
        if s not in SOURCES:
            raise ValueError(f"unknown source {s!r}; choose from {SOURCES}")
    root = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="ftf-scan-"))
    summary = _scan(root, list(sources or SOURCES), keywords=keywords, since=since, **options)
    out_ads = []
    for cap, inc in summary["incidents"]:
        ad = json.loads((root / cap / "ad.json").read_text(encoding="utf-8"))
        out_ads.append(ScannedAd(ad["key"], ad["stable_id"], ad["source"], ad["ad_id"], cap, root / cap, ad, inc))
    return out_ads


def queue(out: Path) -> list[dict]:
    """Every queue item under `out` (dicts with key, source, ad_id, incident_id, capture_dir, status, …)."""
    return Store(Path(out)).read_queue()


def pending(out: Path) -> list[dict]:
    """Queue items still waiting for assignment."""
    return [i for i in queue(out) if i.get("status") == "pending"]


def mark(out: Path, keys: list[str], status: str, wait: float = 300.0) -> int:
    """Set the status (pending / assigned / rejected) of queue items by key; returns how many changed.

    Waits up to `wait` seconds while a scan holds the queue, then raises `Locked`.
    """
    return set_queue_status(Path(out), keys, status, wait=wait)


__all__ = ["ScannedAd", "SOURCES", "QUEUE_STATUSES", "Locked", "scan", "queue", "pending", "mark", "load_incident"]
