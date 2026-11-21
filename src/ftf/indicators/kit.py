"""Kit and page-structure fingerprints (HACK-11).

Fingerprints meant to survive domain rotation and the kits' own randomisation (renamed asset
files, renamed CSS classes, per-deployment ids). Everything is hashed from content or structure,
never from a file or class name. Captured files are parsed as inert bytes and text.

Kinds emitted (layer / level / meaning):

| kind | layer | level | meaning |
|---|---|---|---|
| `asset_sha256` | kit | landing | SHA-256 of a saved asset file or of a HAR JS/CSS/image/font body |
| `asset_norm_sha256` | kit | landing | SHA-256 of a text asset with comments and all whitespace removed |
| `inline_script_sha256` | kit | landing | SHA-256 of a normalised inline script (per-deployment literals, random identifiers, numbers replaced) |
| `dom_skeleton_sha256` | kit | landing | SHA-256 of the tag skeleton (tags only, no classes, ids, attributes or text) |
| `dom_skeleton_lsh` | kit | landing | MinHash LSH band value `bNN:<hex>` of the skeleton's tag 6-shingles; near-identical skeletons share a band |
| `page_url_shape` | landing | platform | page URL parameter names, sorted, with value type tokens (no values) |
| `form_action_shape` | landing | platform | the same for form actions |
| `third_party_host` | infrastructure | platform | host referenced in HTML or HAR outside the page's registered domains (the landing and redirect chain from `_pages.page_chain`, shared with domains.py) |
| `third_party_domain` | infrastructure | platform | registered domain of such a host |

Levels are the coarsest plausible ones: one asset hash can name a landing build (template
images) or a whole platform (an SDK bundle). Linking re-levels values from cluster-level
frequency; public libraries and CDNs are removed by the HACK-12 stop-list in `extract_all`.
`observed` is `live` for values read from HAR traffic and `static` for saved files.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from datasketch import MinHash

from ftf import __version__
from ftf.contract import Artefact, ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.enrich.normalise import hostname, registrable_domain
from ftf.indicators import _kitnorm as kn
from ftf.indicators import _pages

EXTRACTOR = f"kit@{__version__}"
log = logging.getLogger(__name__)

MIN_ASSET_BYTES = 100  # below this (tracking pixels, empty stubs) a hash says nothing about a kit
MIN_SKELETON_TAGS = 30
MIN_SCRIPT_CHARS = 40
LSH_PERM = 128
LSH_BANDS = 16  # 16 bands x 8 rows: P(share a band) ~ 0.85 at Jaccard 0.76, ~0.001 at 0.3
LSH_ROWS = LSH_PERM // LSH_BANDS
SHINGLE = 6

_ASSET_MIME = ("javascript", "ecmascript", "css", "image/", "font/", "woff", "opentype", "truetype")
_ASSET_EXT = (".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".woff", ".woff2", ".ttf", ".otf", ".ico")

_READ = {ArtefactType.asset, ArtefactType.html, ArtefactType.har, ArtefactType.mhtml, ArtefactType.url}

_KINDS: dict[str, tuple[Layer, Level]] = {
    "asset_sha256": (Layer.kit, Level.landing),
    "asset_norm_sha256": (Layer.kit, Level.landing),
    "inline_script_sha256": (Layer.kit, Level.landing),
    "dom_skeleton_sha256": (Layer.kit, Level.landing),
    "dom_skeleton_lsh": (Layer.kit, Level.landing),
    "page_url_shape": (Layer.landing, Level.platform),
    "form_action_shape": (Layer.landing, Level.platform),
    "third_party_host": (Layer.infrastructure, Level.platform),
    "third_party_domain": (Layer.infrastructure, Level.platform),
}


@dataclass
class _Found:
    """Indicators of one capture plus the URLs needed to decide first vs third party."""

    capture_id: str
    indicators: list[Indicator] = field(default_factory=list)
    own_urls: list[str] = field(default_factory=list)  # URLs of the page itself (from _pages.page_chain)
    hosts: list[tuple[str, Observed, str, str]] = field(default_factory=list)  # url, observed, artefact, locator

    def add(self, kind: str, value: str, observed: Observed, art: str, locator: str | None = None) -> None:
        layer, level = _KINDS[kind]
        self.indicators.append(
            Indicator(
                id=f"kit{len(self.indicators)}",
                capture_id=self.capture_id,
                layer=layer,
                kind=kind,
                level=level,
                value=value,
                observed=observed,
                provenance=Provenance(artefact_id=art, locator=locator),
                extractor=EXTRACTOR,
            )
        )


def extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]:
    found = _Found(capture.id)
    for art in capture.artefacts:
        if art.type not in _READ:
            continue  # screenshots, videos, ad-library records: not kit content
        data = _read(root, art)
        if data is None:
            continue
        if art.type is ArtefactType.asset:
            _asset(found, data, Observed.static, art, None)
        elif art.type is ArtefactType.html:
            _document(found, data, Observed.static, art, None, own=True)
        elif art.type is ArtefactType.har:
            _har(found, data, art)
        elif art.type is ArtefactType.mhtml:
            _mhtml(found, data, art)
        elif art.type is ArtefactType.url:
            for m in kn.URL_IN_TEXT.finditer(data[:65536].decode("utf-8", "replace")):
                _page_shape(found, m.group(0), Observed.static, art, "url")
    found.own_urls = [p.url for p in _pages.page_chain(capture, root)]
    _third_party(found)
    return found.indicators


_read = _pages.read


# --------------------------------------------------------------------------- assets


def _asset(found: _Found, data: bytes, observed: Observed, art: Artefact, locator: str | None) -> None:
    if len(data) < MIN_ASSET_BYTES:
        return
    found.add("asset_sha256", kn.sha256(data), observed, art.id, locator)
    if kn.sniff(data) in ("text", "html"):
        norm = kn.normalise_text(data.decode("utf-8", "replace"))
        if len(norm) >= MIN_SCRIPT_CHARS:
            found.add("asset_norm_sha256", kn.sha256(norm.encode()), observed, art.id, locator)


# --------------------------------------------------------------------------- documents


def _document(found: _Found, data: bytes, observed: Observed, art: Artefact, locator: str | None, *, own: bool) -> None:
    root = kn.parse_html(data)
    where = (locator + " ") if locator else ""
    if own:
        for url, loc in kn.page_urls(data, root):
            _page_shape(found, url, observed, art, where + loc)
    if root is None:
        return
    tokens = kn.skeleton_tokens(root)
    if sum(1 for t in tokens if t != "/") >= MIN_SKELETON_TAGS:
        found.add("dom_skeleton_sha256", kn.sha256(" ".join(tokens).encode()), observed, art.id, where + "html")
        for band in _lsh_bands(kn.skeleton_shingles(tokens, SHINGLE)):
            found.add("dom_skeleton_lsh", band, observed, art.id, where + "html")
    for n, code in kn.inline_scripts(root):
        norm = kn.normalise_script(code)
        if len(norm) >= MIN_SCRIPT_CHARS:
            found.add("inline_script_sha256", kn.sha256(norm.encode()), observed, art.id, f"{where}script[{n}]")
    for n, action in kn.form_actions(root):
        shape = kn.param_shape(urlsplit(action).query) if "?" in action else None
        if shape:
            found.add("form_action_shape", shape, observed, art.id, f"{where}form[{n}]@action")
    for url, loc in kn.html_urls(root):
        found.hosts.append((url, observed, art.id, where + loc))


def _lsh_bands(shingles: set[str]) -> list[str]:
    mh = MinHash(num_perm=LSH_PERM, seed=1)
    for s in shingles:
        mh.update(s.encode())
    values = mh.hashvalues
    out = []
    for b in range(LSH_BANDS):
        chunk = values[b * LSH_ROWS : (b + 1) * LSH_ROWS]
        out.append(f"b{b:02d}:{kn.sha256(chunk.tobytes())[:16]}")
    return out


def _page_shape(found: _Found, url: str, observed: Observed, art: Artefact, locator: str) -> None:
    shape = kn.param_shape(urlsplit(url).query)
    if shape:
        found.add("page_url_shape", shape, observed, art.id, locator)


# --------------------------------------------------------------------------- HAR


def _har(found: _Found, data: bytes, art: Artefact) -> None:
    try:
        log = json.loads(data.decode("utf-8", "replace"))["log"]
        entries = log.get("entries") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    main_seen = False
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        loc = f"entries[{i}]"
        req, resp = _dict(entry.get("request")), _dict(entry.get("response"))
        url = req.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        found.hosts.append((url, Observed.live, art.id, loc + ".request.url"))
        content = _dict(resp.get("content"))
        mime = str(content.get("mimeType") or "").lower()
        is_doc = entry.get("_resourceType") == "document" or (not main_seen and "html" in mime)
        if is_doc:
            main_seen = True
            _page_shape(found, url, Observed.live, art, loc + ".request.url")
        body = _body(content)
        if not body:
            continue
        if is_doc or (mime.startswith("text/html") and kn.sniff(body) == "html"):
            _document(found, body, Observed.live, art, loc + ".response", own=False)
        elif _is_asset(url, mime, body):
            _asset(found, body, Observed.live, art, loc + ".response")


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _body(content: dict) -> bytes | None:
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError):
            return None
    return text.encode("utf-8", "surrogatepass")


def _is_asset(url: str, mime: str, body: bytes) -> bool:
    if any(m in mime for m in _ASSET_MIME):
        return True
    if urlsplit(url).path.lower().endswith(_ASSET_EXT):
        return True
    return kn.sniff(body) in ("image", "font")


# --------------------------------------------------------------------------- MHTML


def _mhtml(found: _Found, data: bytes, art: Artefact) -> None:
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception:  # malformed captures must not stop the run
        return
    first_html = True
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        try:
            body = part.get_payload(decode=True) or b""
        except Exception:
            continue
        loc = f"part[{i}]"
        where = str(part.get("Content-Location") or "")
        if part.get_content_type() == "text/html":
            if first_html and where.startswith(("http://", "https://")):
                _page_shape(found, where, Observed.static, art, loc + "@content-location")
            _document(found, body, Observed.static, art, loc, own=first_html)
            first_html = False
        else:
            if where.startswith(("http://", "https://")):
                found.hosts.append((where, Observed.static, art.id, loc + "@content-location"))
            _asset(found, body, Observed.static, art, loc)


# --------------------------------------------------------------------------- hosts


def _third_party(found: _Found) -> None:
    own = {d for d in (registrable_domain("https:" + u if u.startswith("//") else u) for u in found.own_urls) if d}
    for url, observed, art_id, loc in found.hosts:
        if url.startswith("//"):
            url = "https:" + url
        host = hostname(url)
        if host is None:
            continue
        domain = registrable_domain(host)
        if domain is None or domain in own:
            continue
        found.add("third_party_host", host, observed, art_id, loc)
        found.add("third_party_domain", domain, observed, art_id, loc)


def kinds() -> dict[str, tuple[Layer, Level]]:
    """Kind -> (layer, level) for every kind this module emits."""
    return dict(_KINDS)


__all__ = ["extract", "kinds", "EXTRACTOR"]

