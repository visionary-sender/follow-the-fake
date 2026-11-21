"""Safe URL fetcher: capture a page, its traffic, HTML, screenshot and redirects in a locked-down browser.

Library entry points for other stages (e.g. the demo's analyze command):

    from ftf.fetch import fetch_url, fetch_urls
    r = fetch_url("https://example.org/", "captures/")        # -> FetchResult
    r.capture_dir, r.capture_id, r.outcome, r.category, r.manifest
    results = fetch_urls(urls, "captures/")                   # batch; also writes captures/dataset.json

The stable key is the capture folder (`capture_id`). `capture.json` also holds a contract
Incident; that copy is provisional: the canonical Incident and its ids come from Ingestion's
adapter reading the folder. Dead and cloaked pages are results too (see `outcome`/`category`);
nothing here raises for an unreachable page.

Needs the `fetch` extra (`pip install 'ftf[fetch]'` plus `playwright install --only-shell chromium`).
Importing this package does not import Playwright.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ftf.fetch.browser import FetchConfig


@dataclass(frozen=True)
class FetchResult:
    capture_dir: Path
    capture_id: str
    outcome: str  # e.g. live, bot_wall, decoy, dns_failure (see ftf.fetch.classify)
    category: str  # live | cloaked | dead
    manifest: dict  # the parsed capture.json


def fetch_url(url: str, out_root: str | Path, config: "FetchConfig | None" = None) -> FetchResult:
    """Capture `url` into `<out_root>/<capture_id>/` in an isolated, time-limited process."""
    from ftf.fetch.runner import fetch_isolated

    out_root = Path(out_root)
    m = fetch_isolated(url, out_root, config)
    return FetchResult(out_root / m["capture_id"], m["capture_id"], m["outcome"], m["category"], m)


def fetch_urls(urls: Iterable[str], out_root: str | Path, config: "FetchConfig | None" = None) -> list[FetchResult]:
    """Capture several URLs one after another, then refresh `<out_root>/dataset.json`."""
    from ftf.fetch.capture import write_dataset

    results = [fetch_url(u, out_root, config) for u in urls]
    write_dataset(Path(out_root))
    return results
