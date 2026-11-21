"""File-level helpers: artefact types, content hashes and inert page metadata.

Captured files are hostile. Everything here reads them as bytes, JSON or e-mail headers and
never executes, renders or imports them. Only small heads are read for HTML and MHTML.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit

from ftf.contract import ArtefactType

IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"})
VIDEO_EXT = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"})
HTML_EXT = frozenset({".html", ".htm", ".xhtml"})
MHTML_EXT = frozenset({".mhtml", ".mht"})
PAGE_EXT = HTML_EXT | MHTML_EXT | {".har"}
# Browser suffixes for partially saved or renamed files (Polish Chrome saves scripts as `.pobrane`).
_SAVE_SUFFIXES = (".pobrane", ".download", ".crdownload", ".part")
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

_SAVED_FROM = re.compile(rb"<!--\s*saved from url=\(\d+\)\s*([^\s>]+?)\s*-->", re.I)
_DATE = re.compile(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)")
# Markers of scam tracking or platform code. A saved page on a scam host without any of them is
# the cloaker's white page (see the decoy rule in ftf.ingest).
_TRACKER_MARKERS = (b"window.aio", b"fbq(", b"facebook.com/tr", b"BINOM", b"gtag(", b"clickid", b"connect.facebook.net")


def clean_name(name: str) -> str:
    low = name.lower()
    for suf in _SAVE_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def ext(path: Path | str) -> str:
    name = clean_name(Path(path).name).lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def artefact_type(path: Path, *, in_assets: bool, ad_side: bool) -> ArtefactType:
    """Type of a file. ``in_assets``: under a saved page's resource folder. ``ad_side``: images are ad screenshots."""
    name = clean_name(path.name).lower()
    if in_assets:
        return ArtefactType.asset
    e = ext(path)
    if name.endswith(".info.json") or name in ("ad.json",):
        return ArtefactType.ad_library
    if e == ".har":
        return ArtefactType.har
    if e in HTML_EXT:
        return ArtefactType.html
    if e in MHTML_EXT:
        return ArtefactType.mhtml
    if e in IMAGE_EXT:
        return ArtefactType.ad_screenshot if ad_side else ArtefactType.screenshot
    if e in VIDEO_EXT:
        return ArtefactType.video
    if e == ".txt" and name == "url.txt":
        return ArtefactType.url
    if e in (".txt", ".sha256", ".md5"):
        return ArtefactType.text
    return ArtefactType.other


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def date_in_name(name: str) -> str | None:
    m = _DATE.search(name)
    if not m:
        return None
    y, mo, d = m.groups()
    if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
        return f"{y}-{mo}-{d}"
    return None


@dataclass
class PageInfo:
    """What a page file says about the visit it records."""

    url: str | None = None  # page URL (never written to outputs; may carry tokens or local paths)
    scheme: str | None = None  # "http", "https", "file", ...
    started: str | None = None  # ISO timestamp of the visit, when recorded
    markers: bool | None = None  # tracker/platform markers present (HTML only)
    error: str | None = None

    @property
    def host(self) -> str | None:
        return host_of(self.url)

    @property
    def local_file(self) -> bool:
        return self.scheme == "file"

    @property
    def day(self) -> str | None:
        return self.started[:10] if self.started and _DATE.match(self.started[:10]) else None


def _scheme(url: str | None) -> str | None:
    if not url or ":" not in url:
        return None
    return url.split(":", 1)[0].lower()


def har_info(path: Path) -> PageInfo:
    """Page URL and start time of a HAR: the first page, else the first HTML document, else the first entry."""
    try:
        data = json.loads(path.read_bytes())
        log = data["log"]
        entries = log.get("entries") or []
        pages = log.get("pages") or []
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return PageInfo(error=f"unreadable HAR: {type(e).__name__}")
    url = None
    started = None
    if pages and isinstance(pages[0], dict):
        started = pages[0].get("startedDateTime")
        title = pages[0].get("title")
        if isinstance(title, str) and re.match(r"^[a-z]+:", title):
            url = title
    if url is None:
        docs = [e for e in entries if isinstance(e, dict) and "html" in str(((e.get("response") or {}).get("content") or {}).get("mimeType", ""))
                and str((e.get("request") or {}).get("method", "GET")).upper() == "GET"]
        first = docs[0] if docs else (entries[0] if entries and isinstance(entries[0], dict) else None)
        if first is not None:
            url = (first.get("request") or {}).get("url")
            started = started or first.get("startedDateTime")
    if not isinstance(url, str):
        url = None
    if not isinstance(started, str):
        started = None
    return PageInfo(url=url, scheme=_scheme(url), started=started)


def html_info(path: Path) -> PageInfo:
    try:
        with path.open("rb") as f:
            head = f.read(4096)
            rest = head + f.read(4 << 20)
    except OSError as e:
        return PageInfo(error=f"unreadable HTML: {type(e).__name__}")
    m = _SAVED_FROM.search(head)
    url = m.group(1).decode("utf-8", "replace") if m else None
    markers = any(mk in rest for mk in _TRACKER_MARKERS)
    return PageInfo(url=url, scheme=_scheme(url), markers=markers)


def mhtml_info(path: Path) -> PageInfo:
    """Read only the MIME headers of an MHTML archive (stdlib ``email``); parts are not decoded."""
    try:
        with path.open("rb") as f:
            head = f.read(64 << 10)
    except OSError as e:
        return PageInfo(error=f"unreadable MHTML: {type(e).__name__}")
    end = head.find(b"\r\n\r\n")
    if end < 0:
        end = head.find(b"\n\n")
    msg = email.message_from_bytes(head[: end if end >= 0 else len(head)], policy=email.policy.compat32)
    url = msg.get("Snapshot-Content-Location") or msg.get("Content-Location")
    started = None
    if msg.get("Date"):
        try:
            started = email.utils.parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError, IndexError):
            started = None
    url = str(url).strip() if url else None
    return PageInfo(url=url, scheme=_scheme(url), started=started)


def page_info(path: Path, atype: ArtefactType) -> PageInfo | None:
    if atype is ArtefactType.har:
        return har_info(path)
    if atype is ArtefactType.html:
        return html_info(path)
    if atype is ArtefactType.mhtml:
        return mhtml_info(path)
    return None


def timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0
