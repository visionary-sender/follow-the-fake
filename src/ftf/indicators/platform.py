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

import logging
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

from ftf import __version__
from ftf.contract import Artefact, ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.indicators import _har, _html, _pages
from ftf.indicators._ids import CONTACT_KINDS, KINDS, Hit, clean, norm_path, scan

EXTRACTOR = f"platform@{__version__}"
log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_HAR_BYTES = 512 * 1024 * 1024

_BINARY_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|avif|ico|bmp|svgz|mp4|webm|mov|m4v|ts|mp3|wav|ogg|woff2?|ttf|otf|eot|pdf|zip|gz|wasm)$", re.I
)
# Public libraries, matched on the file name with optional version/min/bundle suffixes only, so a
# kit's own `slick-init.js` or `utils.js` is still read. Identifiers inside a library describe
# the library, not the scam.
_LIBRARY = re.compile(
    r"^(jquery|jquery-ui|jquery\.\w+|bootstrap|fbevents|shaka-player|sweetalert2?|moment|lazysizes|embla-carousel|"
    r"swiper|swiper-bundle|slick|owl\.carousel|hls|video|intltelinput|intl-tel-input|analytics|gtm|gtag|recaptcha__\w+|"
    r"react|react-dom|vue|axios|lodash|popper|gsap)"
    r"([.\-](min|slim|prod|production|development|runtime|global|bundle|umd|esm|js|\d[\w.]*))*\.js$",
    re.I,
)
_LIBRARY_PATH = re.compile(r"/(intl-tel-input|intltelinput)[^/]*/.*utils\.js$|/signals/(config|plugins)/|/fbevents", re.I)


def _basename(path: str) -> str:
    name = path.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    return re.sub(r"\.pobrane$", "", name, flags=re.I)


def _is_library(path_or_url: str) -> bool:
    p = re.sub(r"\.pobrane$", "", path_or_url.split("?", 1)[0], flags=re.I)
    return bool(_LIBRARY.search(_basename(p)) or _LIBRARY_PATH.search(p))


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
        # Contact values the collector itself sent (form input): never reported as the operator's.
        self.submitted: set[tuple[str, str]] = set()

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

    def sent(self, text: str, artefact: Artefact, locator: str) -> None:
        """What the collector sent (request URL, body, followed redirect): live, without contacts."""
        for hit in scan(text, contacts=False):
            self.add(hit, Observed.live, artefact, locator)
        for t in (text, unquote_plus(text)):
            for hit in scan(t):
                if hit.kind in CONTACT_KINDS:
                    self.submitted.add((hit.kind, clean(hit.value)))

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

    def indicators(self) -> list[Indicator]:
        return [i for (kind, value, _), i in self.out.items() if (kind, value) not in self.submitted]


_BEACON_HOSTS = re.compile(
    r"(^|\.)(facebook\.com|facebook\.net|google-analytics\.com|googletagmanager\.com|doubleclick\.net|google\.com|"
    r"tiktok\.com|yandex\.(ru|com)|snapchat\.com|sc-static\.net|twitter\.com|x\.com)$"
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
        sink.sent(e.url, artefact, f"{where}.request.url")
        if e.post_text:
            sink.sent(e.post_text, artefact, f"{where}.request.postData")
            fields = _html.form_body_fields(e.post_text, e.post_mime)
            if fields and not _third_party_beacon(e.url):
                sink.add(Hit("form_field_set", fields), Observed.live, artefact, f"{where}.request.postData")
        is_form = "form-urlencoded" in e.post_mime or "multipart/form-data" in e.post_mime
        if e.method == "POST" and is_form and not _third_party_beacon(e.url):
            if p := norm_path(e.url):
                sink.add(Hit("form_endpoint", p), Observed.live, artefact, f"{where}.request.url")
        if gw := _capi_gateway(e):
            sink.add(Hit("capi_gateway", gw), Observed.live, artefact, f"{where}.request.url")
        if e.location:
            sink.sent(e.location, artefact, f"{where}.response.redirect")
        if e.body and not _is_library(e.url):
            sink.page(e.body, artefact, f"{where}.response.content", html="html" in e.body_mime.lower())


def _from_mhtml(sink: _Sink, artefact: Artefact, path: Path) -> None:
    try:
        if path.stat().st_size > MAX_HAR_BYTES:
            return
        data = path.read_bytes()
    except OSError:
        return
    for i, ctype, location, text in _html.mhtml_parts(data):
        if location and _is_library(location):
            continue
        sink.page(text, artefact, f"mhtml:part[{i}]", html=ctype == "text/html")


_PIXEL_CONFIG = re.compile(r"fbq\.registerPlugin|fbq\.loadPlugin|config\.set|\bfbq\b|instance\.optIn")


def _from_asset(sink: _Sink, artefact: Artefact, path: Path) -> None:
    rel = artefact.path or ""
    name = _basename(rel)
    if _BINARY_EXT.search(name) or name.lower().endswith(".css") or _is_library(rel):
        return
    text = _read_text(path)
    if text is None:
        return
    # Meta pixel configs are saved as `<page>_files/<pixel id>`; the name counts only if the
    # file really is a signals config, not a timestamp-named resource.
    if re.fullmatch(r"\d{15,16}", name) and "_files/" in rel and _PIXEL_CONFIG.search(text[:200000]):
        sink.add(Hit("meta_pixel", name), Observed.static, artefact, "filename")
    sink.page(text, artefact, "file", html=name.lower().endswith((".html", ".htm")))


def _from_artefact(sink: _Sink, artefact: Artefact, path: Path) -> None:
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
    elif t in (ArtefactType.text, ArtefactType.other):
        if not _BINARY_EXT.search(artefact.path or ""):
            text = _read_text(path)
            if text is not None:
                sink.page(text, artefact, "file", html=False)


def _from_url(sink: _Sink, artefact: Artefact, root: Path) -> None:
    """A URL-list member: only its own line of the shared list file is scanned (see `_pages.url_text`)."""
    if _BINARY_EXT.search(artefact.path or ""):
        return
    data = _pages.url_text(root, artefact)
    if data is None or len(data) > MAX_TEXT_BYTES or b"\x00" in data[:4096]:
        return
    sink.page(data.decode("utf-8", "replace"), artefact, "file", html=False)


def extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]:
    sink = _Sink(capture)
    root = Path(root)
    for artefact in capture.artefacts:
        if not artefact.path:
            continue
        try:
            if artefact.type is ArtefactType.url:
                _from_url(sink, artefact, root)
            else:
                _from_artefact(sink, artefact, root / artefact.path)
        except Exception as exc:  # a hostile or broken file must not stop the dataset
            log.warning("platform: skipped artefact %s (%s)", artefact.id, type(exc).__name__)
    return sink.indicators()
def kinds() -> Iterator[tuple[str, str, str]]:
    """(kind, layer, level) for every kind this extractor can emit."""
    for kind, (layer, level) in sorted(KINDS.items()):
        yield kind, layer, level
