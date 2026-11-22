"""The page's own URLs: one ordered main-frame chain per capture, shared by `kit` and `domains`.

`page_chain(capture, root)` is the single answer to "which URLs is this page itself": kit.py's
own-versus-third-party split and domains.py's landing and redirect-chain kinds both come from it,
so they cannot disagree. Every URL is an `http(s)` URL; `file://`, `about:` and local paths are
never page URLs (those are collector traces, see collector.py).

Sources, in artefact order:

| artefact | URLs | role | observed |
|---|---|---|---|
| HAR | top-level documents: each page's first document entry, then every 3xx hop from its `Location` / `redirectURL`; `pages[].title` when no document entry exists | last hop of the last page = landing, earlier hops = chain | live |
| url (fetcher `url.txt`) | requested URL, then each main-frame URL, one per line | last line = landing, earlier = chain | live (fetcher) |
| url (any other) | every URL in the file | landing | static |
| HTML | browser "saved from url" comment | landing | static |
| HTML | `link[rel=canonical]`, `meta[og:url]` | landing, only when weak (see below) | static |
| MHTML | `Snapshot-Content-Location` header, first `text/html` part's `Content-Location` | landing | static |
| MHTML | that part's canonical and og:url | landing, only when weak (see below) | static |

Canonical and og:url are weak: a cloned page keeps the canonical of the site it copied. They
count only when the capture has no strong source (HAR, url file, saved-from comment, MHTML
location) or when their registered domain equals a strong one.

Only the top-level chain is own: later document entries (iframes) are not, so kit.py reports
an embedded foreign frame as a third party. Every URL of a non-fetcher url file is a landing:
such a file is the report's own link list for that capture.

A URL that is a landing in one source and a chain hop in another is a landing. Nothing is
executed or rendered: HAR is read as JSON, HTML with lxml, MHTML with `email`.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

from ftf.contract import Artefact, ArtefactType, Capture, Observed
from ftf.enrich.normalise import hostname, registrable_domain
from ftf.indicators import _kitnorm as kn
from ftf.ingest import urllist

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
LANDING = "landing"
CHAIN = "chain"

_HTTP = ("http://", "https://")
_STRONG = ("har", "url", "saved-from", "mhtml")


@dataclass(frozen=True)
class PageUrl:
    """One URL of the page itself."""

    url: str
    role: str  # LANDING or CHAIN
    observed: Observed
    artefact_id: str
    locator: str
    source: str  # har | url | saved-from | canonical | mhtml
    via: str = "start"  # how the browser got here: start, a 3xx status code, or nav (a new page)


@dataclass(frozen=True)
class Hop:
    url: str
    locator: str
    via: str


def is_http(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(_HTTP)


# --------------------------------------------------------------------------- reading


def read(root: Path, art: Artefact) -> bytes | None:
    if not art.path:
        return None
    path = root / art.path
    try:
        if not path.is_file():
            return None
        limit = MAX_ARCHIVE_BYTES if art.type in (ArtefactType.har, ArtefactType.mhtml) else MAX_FILE_BYTES
        if path.stat().st_size > limit:
            log.warning("pages: skipped artefact %s (%d bytes > %d)", art.id, path.stat().st_size, limit)
            return None
        return path.read_bytes()
    except OSError:
        return None


@lru_cache(maxsize=8)
def _list_index(path: str, mtime_ns: int, size: int) -> tuple[str, dict[str, str]]:
    """(sha256 of the whole file, {digest of each URL line: the URL}) for one list file version."""
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest(), {urllist.digest(u): u for u in urllist.urls(data)}


def url_text(root: Path, art: Artefact) -> bytes | None:
    """The text a ``url`` artefact stands for.

    A plain url file (fetcher ``url.txt``, organiser link file) has the digest of the whole file and
    is read whole. A member of a URL list (see :mod:`ftf.ingest.urllist`) names the shared list
    file and carries its URL's digest: only that line counts, so each incident sees its own URL.
    A member whose line is gone (the list was edited after ingest) gives None, never its neighbours.
    """
    if not art.path or not art.sha256:
        return read(root, art)
    path = root / art.path
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file() or st.st_size > MAX_FILE_BYTES:
        return None
    file_digest, index = _list_index(str(path), st.st_mtime_ns, st.st_size)
    if art.sha256 == file_digest:
        return read(root, art)
    url = index.get(art.sha256)
    return None if url is None else url.encode("utf-8")


# --------------------------------------------------------------------------- HAR


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def har_hops(doc: object) -> list[list[Hop]]:
    """Top-level navigation of a parsed HAR: one list of hops per page, in page order.

    A page starts at its first document entry (Chrome `_resourceType`; HARs without it fall back
    to the first entry whose response is HTML). Each 3xx `Location` (or `redirectURL`) is
    followed to the next entry requesting that URL; a target never requested is still a hop.
    """
    log_ = _dict(_dict(doc).get("log"))
    entries = [e if isinstance(e, dict) else {} for e in (log_.get("entries") or [])] if isinstance(log_.get("entries"), list) else []
    pages = [p for p in (log_.get("pages") or []) if isinstance(p, dict)] if isinstance(log_.get("pages"), list) else []
    typed = any("_resourceType" in e for e in entries)

    def is_doc(e: dict) -> bool:
        if typed:
            return e.get("_resourceType") == "document"
        mime = str(_dict(_dict(e.get("response")).get("content")).get("mimeType") or "").lower()
        return "html" in mime

    def url_of(e: dict) -> str | None:
        u = _dict(e.get("request")).get("url")
        return u if is_http(u) else None

    starts: list[tuple[int | None, str | None]] = []  # (entry index, page title url)
    if pages:
        for p in pages:
            pid = p.get("id")
            idx = next((i for i, e in enumerate(entries) if e.get("pageref") == pid and is_doc(e) and url_of(e)), None)
            title = p.get("title") if is_http(p.get("title")) else None
            if idx is not None or title:
                starts.append((idx, title))
    if not starts:
        idx = next((i for i, e in enumerate(entries) if is_doc(e) and url_of(e)), None)
        if idx is not None:
            starts.append((idx, None))

    out: list[list[Hop]] = []
    for n, (idx, title) in enumerate(starts):
        via = "start" if n == 0 else "nav"
        if idx is None:
            out.append([Hop(title, f"pages[{n}].title", via)])
            continue
        hops = [Hop(url_of(entries[idx]), f"entries[{idx}].request.url", via)]
        cur = idx
        seen = {hops[0].url}
        while True:
            resp = _dict(entries[cur].get("response"))
            status = resp.get("status")
            if not (isinstance(status, int) and 300 <= status < 400):
                break
            loc = _location(resp)
            if not loc:
                break
            try:
                target = urljoin(_dict(entries[cur].get("request")).get("url") or "", loc)
            except ValueError:  # malformed Location: the chain ends here
                break
            if not is_http(target) or target in seen:
                break
            seen.add(target)
            nxt = next((i for i in range(cur + 1, len(entries)) if _dict(entries[i].get("request")).get("url") == target), None)
            hops.append(Hop(target, f"entries[{cur}].response.location", str(status)))
            if nxt is None:
                break
            cur = nxt
        out.append(hops)
    return out


def _location(resp: dict) -> str | None:
    for h in resp.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == "location" and isinstance(h.get("value"), str) and h["value"]:
            return h["value"]
    r = resp.get("redirectURL")
    return r if isinstance(r, str) and r else None


@lru_cache(maxsize=8)
def _har_cached(path: str, size: int, mtime: float) -> tuple[tuple[Hop, ...], ...]:
    try:
        doc = json.loads(Path(path).read_bytes().decode("utf-8", "replace"))
        return tuple(tuple(h) for h in har_hops(doc))
    except (OSError, ValueError, TypeError, AttributeError):  # malformed captures must not stop the run
        return ()


def _har_file(root: Path, art: Artefact) -> tuple[tuple[Hop, ...], ...]:
    if not art.path:
        return ()
    path = root / art.path
    try:
        st = path.stat()
        if not path.is_file() or st.st_size > MAX_ARCHIVE_BYTES:
            return ()
    except OSError:
        return ()
    return _har_cached(str(path), st.st_size, st.st_mtime)


# --------------------------------------------------------------------------- MHTML


def mhtml_urls(data: bytes) -> list[tuple[str, str]]:
    """(url, locator): the snapshot header and the first HTML part's Content-Location, plus that
    part's canonical and og:url (locators ending in `link[rel=canonical]` / `meta[og:url]`, weak)."""
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception:  # malformed captures must not stop the run
        return []
    out: list[tuple[str, str]] = []
    snap = str(msg.get("Snapshot-Content-Location") or "").strip()
    if is_http(snap):
        out.append((snap, "@snapshot-content-location"))
    for i, part in enumerate(msg.walk()):
        if part.is_multipart() or part.get_content_type() != "text/html":
            continue
        where = str(part.get("Content-Location") or "").strip()
        if is_http(where):
            out.append((where, f"part[{i}]@content-location"))
        try:
            body = part.get_payload(decode=True) or b""
        except Exception:
            body = b""
        out.extend((u, f"part[{i}] {loc}") for u, loc in kn.page_urls(body, kn.parse_html(body)) if loc != "comment:saved-from")
        break
    return out


# --------------------------------------------------------------------------- the chain


def page_chain(capture: Capture, root: Path, *, fetched: bool = False) -> list[PageUrl]:
    """Ordered own URLs of one capture. `fetched`: the capture comes from our live fetcher."""
    found: list[PageUrl] = []
    for art in capture.artefacts:
        if art.type is ArtefactType.har:
            pages = _har_file(root, art)
            flat = [h for hops in pages for h in hops]
            for n, h in enumerate(flat):
                role = LANDING if n == len(flat) - 1 else CHAIN
                found.append(PageUrl(h.url, role, Observed.live, art.id, h.locator, "har", h.via))
        elif art.type is ArtefactType.url:
            data = url_text(root, art)
            if data is None:
                continue
            urls = [m.group(0) for m in kn.URL_IN_TEXT.finditer(data[:65536].decode("utf-8", "replace"))]
            urls = ["https:" + u if u.startswith("//") else u for u in urls]
            urls = [u for u in urls if is_http(u)]
            for n, u in enumerate(urls):
                if fetched:
                    role, obs = (LANDING if n == len(urls) - 1 else CHAIN), Observed.live
                else:
                    role, obs = LANDING, Observed.static
                found.append(PageUrl(u, role, obs, art.id, f"line[{n}]", "url", "start" if n == 0 else "hop"))
        elif art.type is ArtefactType.html:
            data = read(root, art)
            if data is None:
                continue
            for u, loc in kn.page_urls(data, kn.parse_html(data)):
                src = "saved-from" if loc == "comment:saved-from" else "canonical"
                found.append(PageUrl(u, LANDING, Observed.static, art.id, loc, src))
        elif art.type is ArtefactType.mhtml:
            data = read(root, art)
            if data is None:
                continue
            for u, loc in mhtml_urls(data):
                src = "canonical" if loc.endswith(("link[rel=canonical]", "meta[og:url]")) else "mhtml"
                found.append(PageUrl(u, LANDING, Observed.static, art.id, loc, src))
    strong = {registrable_domain(p.url) for p in found if p.source in _STRONG} - {None}
    if strong:
        found = [p for p in found if p.source != "canonical" or registrable_domain(p.url) in strong]
    landing_hosts = {hostname(p.url) for p in found if p.role == LANDING}
    return [p if p.role == LANDING or hostname(p.url) not in landing_hosts else _as_landing(p) for p in found]


def _as_landing(p: PageUrl) -> PageUrl:
    return PageUrl(p.url, LANDING, p.observed, p.artefact_id, p.locator, p.source, p.via)


def own_domains(chain: list[PageUrl]) -> set[str]:
    """Registered domains of every own URL (landing and chain)."""
    return {d for d in (registrable_domain(p.url) for p in chain) if d}


__all__ = ["PageUrl", "Hop", "page_chain", "har_hops", "mhtml_urls", "own_domains", "read", "is_http", "LANDING", "CHAIN"]
