"""Which domains the organiser's data names: the pages captured in HAR files, and the scam link list.

The data is read as inert bytes and text. HAR files are parsed as JSON data only: nothing in
them is executed, imported, rendered or fetched. Hosts that appear inside a captured page
(CDNs, trackers, platforms, video players) are deliberately out of scope. Only the page a
capture was taken from, and the links the organiser listed as scams, are looked up.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ftf.enrich.normalise import registrable_domain

MAX_FILE_BYTES = 100 * 1024 * 1024
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


@dataclass
class Scope:
    domains: list[str] = field(default_factory=list)
    sources: dict[str, list[str]] = field(default_factory=dict)  # domain -> where it was found
    counts: Counter[str] = field(default_factory=Counter)
    skipped: list[str] = field(default_factory=list)  # files that could not be read as data


def page_urls(har: Any) -> list[str]:
    """Page URLs of one HAR: the `pages[].title` URLs, else the first request URL.

    The first request is not always the page: a player or CDN can load before the page
    document, so the page list is preferred when it holds URLs.
    """
    try:
        log = har["log"]
    except (KeyError, TypeError):
        return []
    pages = log.get("pages") if isinstance(log, dict) else None
    if not isinstance(pages, list):
        pages = []
    titles = [p.get("title") for p in pages if isinstance(p, dict)]
    urls = [t for t in titles if isinstance(t, str) and t.lower().startswith(("http://", "https://"))]
    if urls:
        return urls
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return []
    documents = [e for e in entries if isinstance(e, dict) and e.get("_resourceType") == "document"]
    chosen = documents[0] if documents else (entries[0] if entries and isinstance(entries[0], dict) else None)
    request = chosen.get("request") if isinstance(chosen, dict) else None
    url = request.get("url") if isinstance(request, dict) else None
    return [url] if isinstance(url, str) else []


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def organiser_domains(root: Path, *, tld_cache: Path | None = None) -> Scope:
    """Registrable domains named by the organiser's HAR page URLs and scam link lists under `root`."""
    scope = Scope()
    found: dict[str, set[str]] = {}

    def add(url: str, origin: str) -> None:
        name = registrable_domain(url, tld_cache=tld_cache)
        if name is None:
            return
        found.setdefault(name, set()).add(origin)
        scope.counts[origin] += 1

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for filename in sorted(filenames):
            path = Path(dirpath, filename)
            lower = filename.lower()
            if lower.endswith(".har"):
                text = _read_text(path)
                try:
                    har = json.loads(text) if text is not None else None
                except ValueError:
                    har = None
                if har is None:
                    scope.skipped.append(str(path.relative_to(root)))
                    continue
                try:
                    urls = page_urls(har)
                except (TypeError, AttributeError, KeyError):
                    scope.skipped.append(str(path.relative_to(root)))
                    continue
                for url in urls:
                    add(url, "har_page")
            elif lower.startswith("scam_links") and lower.endswith(".txt"):
                text = _read_text(path)
                if text is None:
                    scope.skipped.append(str(path.relative_to(root)))
                    continue
                for url in _URL.findall(text):
                    add(url.rstrip(".,;)"), "scam_link_list")

    scope.domains = sorted(found)
    scope.sources = {name: sorted(origins) for name, origins in found.items()}
    return scope
