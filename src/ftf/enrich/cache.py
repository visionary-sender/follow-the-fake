"""One JSON file of lookup answers, keyed by (source, domain), each with a fetch time and a TTL.

The file is also the export: it can be copied into a container and read offline. Saves are
atomic (write to a temp file, then rename), so an interrupted run never leaves a half file.
Unreadable files are ignored with a warning rather than raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, _TIME_FORMAT).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Entry:
    source: str
    domain: str
    status: str
    data: dict[str, Any]
    fetched_at: datetime
    expires_at: datetime
    url: str | None

    def fresh(self, now: datetime) -> bool:
        return now < self.expires_at


class Cache:
    """In-memory view of the cache file. Call `save()` to write it back."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.warnings: list[str] = []
        self._entries: dict[str, dict[str, dict[str, Any]]] = {}
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
                    raise ValueError(f"unsupported schema {raw.get('schema') if isinstance(raw, dict) else raw!r}")
                self._entries = raw["entries"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.warnings.append(f"cache {path} ignored: {exc}")
                self._entries = {}

    def __len__(self) -> int:
        return sum(len(by_domain) for by_domain in self._entries.values())

    def get(self, source: str, domain: str) -> Entry | None:
        record = self._entries.get(source, {}).get(domain)
        if record is None:
            return None
        try:
            return Entry(
                source=source,
                domain=domain,
                status=record["status"],
                data=record.get("data") or {},
                fetched_at=parse_utc(record["fetched_at"]),
                expires_at=parse_utc(record["expires_at"]),
                url=record.get("url"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def put(
        self,
        source: str,
        domain: str,
        *,
        status: str,
        data: dict[str, Any],
        fetched_at: datetime,
        ttl: timedelta,
        url: str | None = None,
    ) -> None:
        self._entries.setdefault(source, {})[domain] = {
            "status": status,
            "data": data,
            "url": url,
            "fetched_at": utc_iso(fetched_at),
            "expires_at": utc_iso(fetched_at + ttl),
        }

    def save(self) -> None:
        """Write the cache atomically. Does nothing for an in-memory cache."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schema": SCHEMA, "entries": self._entries}, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.chmod(tmp, 0o644)  # mkstemp creates 0600; the file is meant to be copied out
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
