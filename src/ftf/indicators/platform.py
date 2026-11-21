"""Platform, account and tracker identifiers (HACK-10).

Reads a capture's HTML, MHTML, HAR, saved assets and URL/text artefacts as inert text and emits
the identifiers that name actors: platform and tenant config (aio), owners, landings, Meta
pixels and Conversions-API gateways, ad ids and names, tracker instances (decoded xid click
ids), form endpoints and field-name sets, and contact data in code.

`observed` is `live` only for values in recorded traffic: a HAR request URL, request body or
redirect Location. Strings inside saved files or inside HAR response bodies are page code, so
they are `static`. Kind -> layer/level is in `_ids.KINDS`. Domain metadata is `ftf.enrich`'s
job; visitor-side fields (IP, geo, fingerprint, fbclid session) are never emitted here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

from ftf import __version__
from ftf.contract import Artefact, ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.indicators import _har, _html
from ftf.indicators._ids import KINDS, Hit, clean, norm_path, scan

EXTRACTOR = f"platform@{__version__}"

MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_HAR_BYTES = 512 * 1024 * 1024

_BINARY_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|avif|ico|bmp|svgz|mp4|webm|mov|m4v|ts|mp3|wav|ogg|woff2?|ttf|otf|eot|pdf|zip|gz|wasm)$", re.I
)
# Public libraries: identifiers inside them describe the library, not the scam.
_LIBRARY = re.compile(
    r"(jquery|bootstrap|fbevents|shaka-player|sweetalert|moment(\.min)?\.js|lazysizes|embla-carousel|swiper|slick|"
    r"owl\.carousel|hls(\.min)?\.js|video(\.min)?\.js|intltelinput|intl-tel-input|utils\.js|gtag|analytics\.js|gtm\.js|"
    r"recaptcha|polyfill|react(-dom)?(\.production)?(\.min)?\.js|vue(\.runtime)?(\.global)?(\.prod)?(\.min)?\.js)",
    re.I,
)


def _basename(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"\.pobrane$", "", name, flags=re.I)


def _read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


class _Sink:
    """Collects hits as indicators, one per (kind, value, observed), keeping the first provenance."""

    def __init__(self, capture: Capture) -> None:
        self.capture = capture
        self.out: dict[tuple[str, str, Observed], Indicator] = {}

    def add(self, hit: Hit, observed: Observed, artefact: Artefact, locator: str) -> None:
        layer, level = KINDS[hit.kind]
        value = clean(hit.value)
        if not value:
            return
        key = (hit.kind, value, observed)
        if key in self.out:
            return
        n = len(self.out)
        self.out[key] = Indicator(
            id=f"{self.capture.id}:platform:{n}",
            capture_id=self.capture.id,
            layer=Layer(layer),
            kind=hit.kind,
            level=Level(level),
            value=value,
            observed=observed,
            provenance=Provenance(artefact_id=artefact.id, locator=locator, note=hit.note),
            extractor=EXTRACTOR,
        )

    def page(self, text: str, artefact: Artefact, where: str, html: bool) -> None:
        """Page code (saved file or response body): always static."""
        cfg = _html.aio_config(text)
        if cfg is not None:
            for hit, path in _html.aio_hits(cfg):
                self.add(hit, Observed.static, artefact, f"{where}:aio{path[1:]}")
        if html:
            for hit, loc in _html.form_hits(text):
                self.add(hit, Observed.static, artefact, f"{where}:{loc}")
        for hit in scan(text):
            self.add(hit, Observed.static, artefact, where)


_BEACON_HOSTS = re.compile(
    r"(^|\.)(facebook\.com|facebook\.net|google-analytics\.com|googletagmanager\.com|doubleclick\.net|google\.com|"
    r"tiktok\.com|clarity\.ms|yandex\.(ru|com)|bing\.com|snapchat\.com|sc-static\.net|taboola\.com|quora\.com|twitter\.com|x\.com)$"
)


def _third_party_beacon(url: str) -> bool:
    """True for analytics/ad beacons that POST form bodies (Meta /tr/, GA collect): not scam forms."""
    host = (urlsplit(url).hostname or "").lower()
    return bool(_BEACON_HOSTS.search(host))


def _capi_gateway(entry: _har.Entry) -> str | None:
    """Host of a Conversions-API gateway: a POST to `/events` whose body names a pixel."""
    if entry.method != "POST" or "pixel_id" not in entry.post_text:
        return None
    sp = urlsplit(entry.url)
    if not sp.path.rstrip("/").endswith("/events") or not sp.hostname:
        return None
    return sp.hostname.lower()


def _from_har(sink: _Sink, artefact: Artefact, path: Path) -> None:
    try:
        if path.stat().st_size > MAX_HAR_BYTES:
            return
    except OSError:
        return
    for e in _har.entries(path):
        where = f"har:entries[{e.index}]"
        for hit in scan(e.url):
            sink.add(hit, Observed.live, artefact, f"{where}.request.url")
        if e.post_text:
            for hit in scan(e.post_text):
                sink.add(hit, Observed.live, artefact, f"{where}.request.postData")
            fields = _html.form_body_fields(e.post_text, e.post_mime)
            if fields and not _third_party_beacon(e.url):
                sink.add(Hit("form_field_set", fields), Observed.live, artefact, f"{where}.request.postData")
        if e.method == "POST" and ("form-urlencoded" in e.post_mime or "multipart/form-data" in e.post_mime) and not _third_party_beacon(e.url):
            if p := norm_path(e.url):
                sink.add(Hit("form_endpoint", p), Observed.live, artefact, f"{where}.request.url")
        if gw := _capi_gateway(e):
            sink.add(Hit("capi_gateway", gw), Observed.live, artefact, f"{where}.request.url")
        if e.location:
            for hit in scan(e.location):
                sink.add(hit, Observed.live, artefact, f"{where}.response.redirect")
        if e.body:
            sink.page(e.body, artefact, f"{where}.response.content", html="html" in e.body_mime.lower())


def _from_mhtml(sink: _Sink, artefact: Artefact, path: Path) -> None:
    try:
        if path.stat().st_size > MAX_HAR_BYTES:
            return
        data = path.read_bytes()
    except OSError:
        return
    for i, ctype, text in _html.mhtml_parts(data):
        sink.page(text, artefact, f"mhtml:part[{i}]", html=ctype == "text/html")


def _from_asset(sink: _Sink, artefact: Artefact, path: Path) -> None:
    name = _basename(artefact.path or "")
    # Meta pixel configs are saved as `<page>_files/<pixel id>`.
    if re.fullmatch(r"\d{15,16}", name) and "_files/" in (artefact.path or ""):
        sink.add(Hit("meta_pixel", name), Observed.static, artefact, "filename")
    if _BINARY_EXT.search(name) or name.lower().endswith(".css") or _LIBRARY.search(name):
        return
    text = _read_text(path)
    if text is not None:
        sink.page(text, artefact, "file", html=name.lower().endswith((".html", ".htm")))


def extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]:
    sink = _Sink(capture)
    root = Path(root)
    for artefact in capture.artefacts:
        if not artefact.path:
            continue
        path = root / artefact.path
        t = artefact.type
        if t is ArtefactType.har:
            _from_har(sink, artefact, path)
        elif t is ArtefactType.mhtml:
            _from_mhtml(sink, artefact, path)
        elif t is ArtefactType.html:
            text = _read_text(path)
            if text is not None:
                sink.page(text, artefact, "file", html=True)
        elif t is ArtefactType.asset:
            _from_asset(sink, artefact, path)
        elif t in (ArtefactType.url, ArtefactType.text, ArtefactType.other):
            if not _BINARY_EXT.search(artefact.path):
                text = _read_text(path)
                if text is not None:
                    sink.page(text, artefact, "file", html=False)
    return list(sink.out.values())


def kinds() -> Iterator[tuple[str, str, str]]:
    """(kind, layer, level) for every kind this extractor can emit."""
    for kind, (layer, level) in sorted(KINDS.items()):
        yield kind, layer, level
