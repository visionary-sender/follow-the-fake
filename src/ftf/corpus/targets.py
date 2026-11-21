"""Turn scanner queue rows and hand-picked lists into fetch targets.

A target is one URL to fetch once per batch. Several queue rows can point at the same URL
(e.g. many Meta ads of one advertiser share a landing page), so targets carry every queue key
that asked for them. The builder later matches fetcher captures back to queue rows through
`requested_url`, using the same functions, so this module is the single place where a queue row
becomes a URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# A bare hostname as the scanner writes it (CERT Polska DomainAddress, Google display domain).
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


@dataclass(frozen=True)
class QueueRow:
    key: str
    source: str
    incident_id: str
    incidents_file: str
    run_id: str
    keyword: str | None
    landing_url: str | None
    landing_domain: str | None

    @classmethod
    def from_json(cls, row: dict) -> "QueueRow":
        return cls(
            key=row["key"],
            source=row["source"],
            incident_id=row["incident_id"],
            incidents_file=row["incidents_file"],
            run_id=row["run_id"],
            keyword=row.get("keyword") or None,
            landing_url=row.get("landing_url") or None,
            landing_domain=row.get("landing_domain") or None,
        )


@dataclass
class Target:
    url: str
    keys: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def read_queue(path: Path, runs: Iterable[str] | None = None) -> list[QueueRow]:
    """Read the scanner's queue.jsonl, keeping only rows from `runs` when given.

    Rows repeated under the same key keep their first occurrence.
    """
    wanted = set(runs) if runs else None
    out: list[QueueRow] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = QueueRow.from_json(json.loads(line))
            if wanted is not None and row.run_id not in wanted:
                continue
            if row.key in seen:
                continue
            seen.add(row.key)
            out.append(row)
    return out


def normalise_host(host: str) -> str | None:
    host = host.strip().strip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host if _HOST.match(host) else None


def target_url(row: QueueRow) -> str | None:
    """The URL to fetch for a queue row: the ad's landing URL, else https://<domain>/."""
    if row.landing_url and row.landing_url.lower().startswith(("http://", "https://")):
        return row.landing_url.strip()
    if row.landing_domain:
        host = normalise_host(row.landing_domain)
        if host:
            return f"https://{host}/"
    return None


def build_targets(rows: Iterable[QueueRow]) -> list[Target]:
    """Unique targets in first-seen order, interleaved by source so every source starts early."""
    by_url: dict[str, Target] = {}
    per_source: dict[str, list[Target]] = {}
    for row in rows:
        url = target_url(row)
        if url is None:
            continue
        t = by_url.get(url)
        if t is None:
            t = by_url[url] = Target(url)
            per_source.setdefault(row.source, []).append(t)
        t.keys.append(row.key)
        if row.source not in t.sources:
            t.sources.append(row.source)
    return list(_round_robin(list(per_source.values())))


def _round_robin(lists: list[list[Target]]) -> Iterator[Target]:
    i = 0
    while any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst):
                yield lst[i]
        i += 1


def split(targets: list[Target], parts: int) -> list[list[Target]]:
    """Deal targets into `parts` lists (for at most `parts` sequential fetch workers)."""
    parts = max(1, parts)
    return [targets[i::parts] for i in range(parts)]


@dataclass(frozen=True)
class PickedPage:
    url: str
    brand: str
    page_kind: str  # e.g. article, home
    note: str | None = None


def read_picked(path: Path) -> list[PickedPage]:
    """A hand-picked list: TSV `url<TAB>brand<TAB>page_kind[<TAB>note]`, `#` comments allowed."""
    out: list[PickedPage] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 3 or not cols[0].lower().startswith(("http://", "https://")):
            raise ValueError(f"{path}:{n}: expected url<TAB>brand<TAB>page_kind[<TAB>note]")
        if cols[0] in seen:
            continue
        seen.add(cols[0])
        out.append(PickedPage(cols[0], cols[1], cols[2], cols[3] if len(cols) > 3 and cols[3] else None))
    return out
