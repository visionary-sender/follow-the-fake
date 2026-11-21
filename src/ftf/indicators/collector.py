"""Collector traces: values that describe the person or tool that captured a page, not the scam.

Every indicator emitted here sits in the `collector` layer at level `capture`, so Linking never uses
it as evidence. Exclusion is decided by *type* (the kinds in `ftf.contract.COLLECTOR_KINDS`), never
by frequency: a collector value shared by many captures is the analyst's browser, and treating it as
an identifier would link everything one analyst captured.

Kinds and where they come from:

- `aio_fingerprint`: the AIO platform's FingerprintJS visitor id (`fingerprint=` in the tracker POSTs,
  `visit.fingerprint` in the `window.aio` JSON of the saved page).
- `browser_fingerprint`: other per-browser ids: Meta's `_fbp` browser cookie (`fbp=`), Chrome's
  `x-client-data` request header.
- `analyst_ip`, `analyst_geo`: the visitor IP and the geo/ASN/time-zone fields that the scam
  platform resolved for it (aio visit JSON, geo-lookup responses, `ip=` parameters). Geo fields are
  only taken from a JSON object that also carries an IP or an ASN (a geo lookup or the aio visit),
  plus the browser time zone the aio SDK posts.
- `user_agent`: `User-Agent` request headers, UA strings in client payloads, the archiver's
  `user_agent` field.
- `viewport`, `screen_size`: window size posted by the aio SDK (`browser_window_width/height`, JSON
  `viewport`), screen size in Meta pixel (`sw`/`sh`) and Google ads (`u_w`/`u_h`) requests.
- `fbclid_session`: the browser-session part of Meta click ids. A current fbclid is a two-character
  prefix plus base64url of a tag-length-value record (`igrd`/`bocl`, `extn`, `aem`, `pdof`, `adid`,
  `srtc`, `app_id`, ...). The ad fields belong to HACK-10 (`platform.py`); the opaque bytes after the
  `app_id` tag carry the browser session and repeat across unrelated ads clicked in one browser.
  Only that tail is emitted. Click ids that do not decode this way are skipped.
- `local_path`, `os_username`: `file://` URIs and Windows drive paths in saved pages and HAR request
  fields (e.g. a page saved from disk), and the user name inside them.
- `capture_time`, `capture_day`: the first HAR `startedDateTime`, MHTML `Date:`, the aio visit's
  `created_at`, the archiver's `timestamp_utc`, screenshot file names; `capture_day` is the date part
  of each, plus the capture's `captured_on`.
- `screenshot_date`: the date in a screenshot's file name ("Zrzut ekranu 2025-01-02 031405.png") or
  its PNG metadata.

Privacy: values that point at a person or a device (IP, geo fields, user name, local path,
fingerprints, fbclid session) are stored only as `sha256:<24 hex>` of `kind, field, value`. Equal
inputs give equal outputs, so the demo can still show "these captures share a collector", but the
raw value never enters a dataset. This is pseudonymisation, not secrecy: an IPv4 hash can be brute-
forced, so the outputs stay private like the raw data. User agents, sizes and times stay readable.

Captured files are parsed as inert text (json, re, email); nothing is executed or fetched.
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from ftf import __version__
from ftf.contract import Artefact, ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance

EXTRACTOR = f"collector@{__version__}"
MAX_BYTES = 300_000_000  # skip absurdly large files rather than load them
MAX_BODY = 5_000_000  # largest HAR body or JSON payload we parse

HASHED = {"aio_fingerprint", "browser_fingerprint", "analyst_ip", "analyst_geo", "os_username", "local_path", "fbclid_session"}

_AIO = re.compile(r"window\.aio\s*=\s*JSON\.parse\('(.*?)'\);", re.S)
_HEX_ID = re.compile(r"^[0-9a-fA-F]{16,64}$")
_FBP = re.compile(r"^fb\.\d\.\d{10,}\.\d+$")
_FBC = re.compile(r"^fb\.\d\.\d{10,}\.(.+)$")
_FILE_URI = re.compile(r"file:///?(?:[A-Za-z](?::|%3A|\|)[^\s\"'<>)&#]*|/[^\s\"'<>)&#]+)", re.I)
_WIN_PATH = re.compile(r"\b[A-Za-z]:(?:\\\\|\\|/)+(?:Users|Documents and Settings)(?:\\\\|\\|/)+[^\\/\s\"'<>]+[^\s\"'<>&#]*", re.I)
_USER_IN_PATH = re.compile(r"(?:^|[\\/:])(?:Users|Documents and Settings|home)(?:\\\\|\\|/|%5C|%2F)+([^\\/\s\"'<>%]+)", re.I)
_GENERIC_USERS = {"public", "default", "default user", "all users", "shared"}
_SHOT_NAME = re.compile(
    r"(?:zrzut[ _]ekranu|screenshot|screen[ _]shot|capture|zrzut)\D{0,6}"
    r"(\d{4})-?(\d{2})-?(\d{2})(?:\D{1,4}(\d{2})[.:\-]?(\d{2})[.:\-]?(\d{2}))?",
    re.I,
)

UA_KEYS = {"useragent", "user_agent", "http_user_agent", "ua"}
IP_KEYS = {"ip", "ip_address", "ipaddress", "client_ip", "clientip", "remote_ip", "user_ip", "visitor_ip", "query", "ip_addr"}
GEO_KEYS = {
    "city", "location_city", "postal", "postal_code", "zip", "zip_code", "location_postal_code", "region",
    "region_name", "regionname", "location_subdivisions", "subdivision", "asn_organization", "asn_org",
    "org", "isp", "as", "asn", "latitude", "longitude", "lat", "lon", "loc", "timezone", "time_zone",
    "location_timezone", "country", "country_code", "countrycode", "country_name", "location_country",
}
FP_KEYS = {"fingerprint", "visitorid", "visitor_id"}
FBP_KEYS = {"fbp", "_fbp"}
SIZE_PAIRS = {
    ("browser_window_width", "browser_window_height"): "viewport",
    ("innerwidth", "innerheight"): "viewport",
    ("viewport_width", "viewport_height"): "viewport",
    ("screen_width", "screen_height"): "screen_size",
    ("screenwidth", "screenheight"): "screen_size",
    ("sw", "sh"): "screen_size",
    ("u_w", "u_h"): "screen_size",
}
TZ_KEYS = {"browser_time_zone", "browser_timezone"}
TIME_KEYS = {"timestamp_utc"}


def pseudonymise(kind: str, value: str, field: str = "") -> str:
    """Stable one-way token for a value that could identify the analyst (see module docstring)."""
    return "sha256:" + hashlib.sha256(f"{kind}\x00{field}\x00{value}".encode()).hexdigest()[:24]


def _ip(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _scalar(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, list) and value and all(isinstance(x, (str, int, float)) for x in value):
        return ",".join(str(x) for x in value)
    return None


def _size(w: object, h: object) -> str | None:
    try:
        wi, hi = int(float(str(w))), int(float(str(h)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 < wi < 20000 and 0 < hi < 20000):
        return None
    return f"{wi}x{hi}"


def fbclid_session(fbclid: str) -> bytes | None:
    """The browser-session tail of a Meta fbclid (the bytes after the `app_id` tag), or None."""
    head = unquote(fbclid).split("_aem_", 1)[0]
    for skip in (2, 0):
        body = head[skip:]
        if len(body) < 40:
            continue
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except (ValueError, TypeError):
            continue
        i = raw.find(b"app_id")
        if i >= 0 and b"adid" in raw[:i] and len(raw) - (i + 6) >= 8:
            return raw[i + 6 :]
    return None


class _Emitter:
    def __init__(self, capture: Capture) -> None:
        self.capture = capture
        self.out: list[Indicator] = []
        self._seen: set[tuple] = set()
        self.times: list[tuple[str, Observed, Provenance]] = []

    def add(self, kind: str, value: str, observed: Observed, art: Artefact | None, locator: str | None,
            field: str = "", note: str | None = None) -> None:
        if not value:
            return
        shown = pseudonymise(kind, value, field) if kind in HASHED else value
        if field and kind == "analyst_geo":
            shown = f"{field}={shown}"
        key = (kind, shown, observed)
        if key in self._seen:
            return
        self._seen.add(key)
        prov = Provenance(artefact_id=art.id if art else None, locator=locator, note=note or (field or None))
        self.out.append(
            Indicator(
                id=f"{self.capture.id}:collector:{len(self.out)}",
                capture_id=self.capture.id,
                layer=Layer.collector,
                kind=kind,
                level=Level.capture,
                value=shown,
                observed=observed,
                provenance=prov,
                extractor=EXTRACTOR,
            )
        )
        if kind == "capture_time":
            day = value[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                self.add("capture_day", day, observed, art, locator)

    # ---- generic payload walker -------------------------------------------------------------

    def walk(self, obj: object, observed: Observed, art: Artefact, locator: str, depth: int = 0) -> None:
        """Pick collector fields out of a decoded JSON value (a client payload or a server answer)."""
        if depth > 12:
            return
        if isinstance(obj, list):
            for n, x in enumerate(obj[:2000]):
                self.walk(x, observed, art, f"{locator}[{n}]", depth + 1)
            return
        if not isinstance(obj, dict):
            return
        low = {str(k).lower(): v for k, v in obj.items()}
        self.fields(low, observed, art, locator)
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                self.walk(v, observed, art, f"{locator}.{k}", depth + 1)
            elif isinstance(v, str) and len(v) < MAX_BODY and v[:1] in "{[":
                try:
                    inner = json.loads(v)
                except (ValueError, RecursionError):
                    continue
                self.walk(inner, observed, art, f"{locator}.{k}", depth + 1)

    def fields(self, low: dict[str, object], observed: Observed, art: Artefact, locator: str) -> None:
        """Collector fields of one flat mapping (lower-cased keys): a JSON object or a parameter list."""
        ip_here = None
        for k in IP_KEYS & low.keys():
            ip = _ip(low[k])
            if ip:
                ip_here = ip
                self.add("analyst_ip", ip, observed, art, f"{locator}.{k}")
        geo_ok = ip_here is not None or "asn_organization" in low or "asn" in low
        if geo_ok:
            for k in sorted(GEO_KEYS & low.keys()):
                v = _scalar(low[k])
                if v:
                    self.add("analyst_geo", v, observed, art, f"{locator}.{k}", field=k)
            if "created_at" in low and ("ip_address" in low or "asn_organization" in low):
                v = _scalar(low["created_at"])
                if v:
                    self.add("capture_time", v, observed, art, f"{locator}.created_at")
        for k in sorted(TZ_KEYS & low.keys()):
            v = _scalar(low[k])
            if v:
                self.add("analyst_geo", v, observed, art, f"{locator}.{k}", field="time_zone")
        for k in UA_KEYS & low.keys():
            v = _scalar(low[k])
            if v and v.startswith("Mozilla/"):
                self.add("user_agent", v, observed, art, f"{locator}.{k}")
        for k in FP_KEYS & low.keys():
            v = _scalar(low[k])
            if v and _HEX_ID.match(v):
                self.add("aio_fingerprint", v.lower(), observed, art, f"{locator}.{k}")
        for k in FBP_KEYS & low.keys():
            v = _scalar(low[k])
            if v and _FBP.match(v):
                self.add("browser_fingerprint", v, observed, art, f"{locator}.{k}", field="fbp")
        if "fbc" in low:
            v = _scalar(low["fbc"])
            m = _FBC.match(v or "")
            if m:
                self.fbclid(m.group(1), observed, art, f"{locator}.fbc")
        if "fbclid" in low:
            v = _scalar(low["fbclid"])
            if v:
                self.fbclid(v, observed, art, f"{locator}.fbclid")
        for (wk, hk), kind in SIZE_PAIRS.items():
            if wk in low and hk in low:
                s = _size(low[wk], low[hk])
                if s:
                    self.add(kind, s, observed, art, f"{locator}.{wk}")
        vp = low.get("viewport")
        if isinstance(vp, dict):
            s = _size(vp.get("width"), vp.get("height"))
            if s:
                self.add("viewport", s, observed, art, f"{locator}.viewport")
        sc = low.get("screen")
        if isinstance(sc, dict):
            s = _size(sc.get("width"), sc.get("height"))
            if s:
                self.add("screen_size", s, observed, art, f"{locator}.screen")
        for k in TIME_KEYS & low.keys():
            v = _scalar(low[k])
            if v:
                self.add("capture_time", v, observed, art, f"{locator}.{k}")

    def fbclid(self, value: str, observed: Observed, art: Artefact, locator: str) -> None:
        tail = fbclid_session(value)
        if tail is not None:
            self.add("fbclid_session", tail.hex(), observed, art, locator)

    def query(self, url: str, observed: Observed, art: Artefact, locator: str) -> None:
        try:
            q = urlsplit(url).query
        except ValueError:
            return
        if q:
            self.fields({k.lower(): v for k, v in parse_qsl(q, keep_blank_values=False)}, observed, art, locator)

    def paths(self, text: str, observed: Observed, art: Artefact, locator: str) -> None:
        """Local paths, also inside percent-encoded values (e.g. a tracker's `url=` parameter)."""
        # Scan only the most-decoded copy: undecoded text is a subset of it, and one pass gives one
        # value per path (a raw %20 would otherwise end the match early on the decoded pass).
        t = text
        for _ in range(2):
            if "%" not in t:
                break
            t = unquote(t)
        uris = [m.span() for m in _FILE_URI.finditer(t)]
        for rx in (_FILE_URI, _WIN_PATH):
            for m in rx.finditer(t):
                if rx is _WIN_PATH and any(a <= m.start() < b for a, b in uris):
                    continue  # the same path inside a file:// URI, already emitted
                path = m.group(0)
                self.add("local_path", path, observed, art, locator)
                u = _USER_IN_PATH.search(path.replace("\\\\", "\\"))
                if u and u.group(1).lower() not in _GENERIC_USERS:
                    self.add("os_username", u.group(1), observed, art, locator)


# ---- per artefact type ------------------------------------------------------------------------


def _read(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _aio_blobs(text: str) -> Iterator[tuple[int, object]]:
    for m in _AIO.finditer(text):
        raw = m.group(1)
        for decode in (
            lambda s: s.encode("latin-1", "backslashreplace").decode("unicode_escape"),
            lambda s: s.replace("\\'", "'"),
        ):
            try:
                yield m.start(), json.loads(decode(raw))
                break
            except (ValueError, UnicodeError, RecursionError):
                continue


def _html_text(em: _Emitter, text: str, observed: Observed, art: Artefact, base: str) -> None:
    for pos, blob in _aio_blobs(text):
        em.walk(blob, observed, art, f"{base}window.aio@{pos}")
    em.paths(text, observed, art, f"{base}text")


def _from_html(em: _Emitter, art: Artefact, data: bytes) -> None:
    _html_text(em, data.decode("utf-8", "replace"), Observed.static, art, "")


def _from_mhtml(em: _Emitter, art: Artefact, data: bytes) -> None:
    try:
        msg = email.message_from_bytes(data, policy=email.policy.compat32)
    except (ValueError, TypeError):
        return
    date = msg.get("Date")
    if date:
        try:
            when = email.utils.parsedate_to_datetime(str(date))
            if when.tzinfo is None:  # "-0000" or no zone: RFC 5322 says UTC, never the host zone
                when = when.replace(tzinfo=timezone.utc)
            em.add("capture_time", when.astimezone(timezone.utc).isoformat(), Observed.static, art, "mhtml:Date")
        except (TypeError, ValueError):
            pass
    for n, part in enumerate(msg.walk()):
        ctype = part.get_content_type()
        if not ctype.startswith("text/"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except (ValueError, TypeError):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        _html_text(em, text, Observed.static, art, f"part[{n}].")
        loc = part.get("Content-Location")
        if loc:
            em.paths(str(loc), Observed.static, art, f"part[{n}].Content-Location")


def _d(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _earlier(t: str, first: str | None) -> bool:
    """True if HAR time `t` is before `first` (compared as instants, not strings)."""
    if first is None:
        return True
    a, b = _instant(t), _instant(first)
    if a is None or b is None:
        return a is not None and b is None
    return a < b


def _instant(t: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _valid(y, mo, d, hh=None, mi=None, ss=None) -> bool:
    try:
        datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return False
    return 2000 <= int(y) <= 2100


def _har_body(entry: dict, which: str) -> str | None:
    if which == "request":
        pd = _d(_d(entry.get("request")).get("postData"))
        text = pd.get("text")
    else:
        content = _d(_d(entry.get("response")).get("content"))
        text = content.get("text")
        if text and content.get("encoding") == "base64":
            try:
                text = base64.b64decode(text).decode("utf-8", "replace")
            except (ValueError, TypeError):
                return None
    if not isinstance(text, str) or len(text) > MAX_BODY:
        return None
    return text


def _from_har(em: _Emitter, art: Artefact, data: bytes) -> None:
    try:
        har = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return
    log = har.get("log") if isinstance(har, dict) else None
    if not isinstance(log, dict):
        return
    live = Observed.live
    first: str | None = None
    for n, page in enumerate(log.get("pages") if isinstance(log.get("pages"), list) else []):
        if not isinstance(page, dict):
            continue
        t = page.get("startedDateTime")
        if isinstance(t, str) and _earlier(t, first):
            first = t
        title = page.get("title")
        if isinstance(title, str):
            em.paths(title, live, art, f"pages[{n}].title")
    for i, entry in enumerate(log.get("entries") if isinstance(log.get("entries"), list) else []):
        if not isinstance(entry, dict):
            continue
        loc = f"entries[{i}]"
        t = entry.get("startedDateTime")
        if isinstance(t, str) and _earlier(t, first):
            first = t
        req = _d(entry.get("request"))
        url = req.get("url") or ""
        if isinstance(url, str):
            em.query(url, live, art, f"{loc}.request.url")
            em.paths(url, live, art, f"{loc}.request.url")
        for h in req.get("headers") or []:
            if not isinstance(h, dict):
                continue
            name = str(h.get("name", "")).lower()
            value = str(h.get("value", ""))
            if name == "user-agent" and value:
                em.add("user_agent", value, live, art, f"{loc}.request.headers.user-agent")
            elif name == "x-client-data" and value:
                em.add("browser_fingerprint", value, live, art, f"{loc}.request.headers.x-client-data", field="x-client-data")
            elif name in ("referer", "origin"):
                em.paths(value, live, art, f"{loc}.request.headers.{name}")
        pd = _d(req.get("postData"))
        params = pd.get("params") if isinstance(pd, dict) else None
        if params:
            flat = {str(p.get("name", "")).lower(): unquote(str(p.get("value", ""))) for p in params if isinstance(p, dict)}
            em.fields(flat, live, art, f"{loc}.request.postData.params")
        body = _har_body(entry, "request")
        if body:
            _payload(em, body, live, art, f"{loc}.request.postData")
            em.paths(body, live, art, f"{loc}.request.postData")
        rbody = _har_body(entry, "response")
        if rbody:
            mime = str(_d(_d(entry.get("response")).get("content")).get("mimeType", "")).lower()
            if "json" in mime or rbody[:1] in "{[":
                _payload(em, rbody, live, art, f"{loc}.response.content")
            elif "html" in mime and "window.aio" in rbody:
                for pos, blob in _aio_blobs(rbody):
                    em.walk(blob, live, art, f"{loc}.response.content.window.aio@{pos}")
    if first:
        em.add("capture_time", first, live, art, "log.startedDateTime(min)")


def _payload(em: _Emitter, text: str, observed: Observed, art: Artefact, locator: str) -> None:
    s = text.strip()
    if s[:1] in "{[":
        try:
            em.walk(json.loads(s), observed, art, locator)
            return
        except (ValueError, RecursionError):
            pass
    if "=" in s and "\n" not in s[:2000]:
        try:
            pairs = parse_qsl(s, keep_blank_values=False)
        except ValueError:
            return
        if pairs:
            em.fields({k.lower(): v for k, v in pairs}, observed, art, locator)


def _png_times(data: bytes) -> Iterator[tuple[str, str]]:
    """(field, text) of PNG tIME and tEXt/iTXt time chunks, read as plain bytes."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"tIME" and len(body) == 7:
            y = int.from_bytes(body[:2], "big")
            yield "tIME", f"{y:04d}-{body[2]:02d}-{body[3]:02d}T{body[4]:02d}:{body[5]:02d}:{body[6]:02d}Z"
        elif ctype in (b"tEXt", b"iTXt"):
            key, _, rest = body.partition(b"\x00")
            if key.lower() in (b"creation time", b"date:create", b"date:modify"):
                if ctype == b"iTXt":
                    rest = rest[2:].split(b"\x00", 2)[-1]
                yield key.decode("latin-1"), rest.decode("utf-8", "replace").strip()
        elif ctype == b"IEND":
            return


def _date_of(text: str) -> str | None:
    m = re.search(r"(\d{4})[-:](\d{2})[-:](\d{2})", text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _from_screenshot(em: _Emitter, art: Artefact, path: Path) -> None:
    m = _SHOT_NAME.search(path.name)
    if m:
        y, mo, d, hh, mi, ss = m.groups()
        if not _valid(y, mo, d, hh, mi, ss):
            m = None
    if m:
        em.add("screenshot_date", f"{y}-{mo}-{d}", Observed.static, art, "filename")
        if hh:
            em.add("capture_time", f"{y}-{mo}-{d}T{hh}:{mi}:{ss} local", Observed.static, art, "filename")
        else:
            em.add("capture_day", f"{y}-{mo}-{d}", Observed.static, art, "filename")
    data = _read(path)
    if data:
        for field, text in _png_times(data[:2_000_000]):
            day = _date_of(text)
            if day:
                em.add("screenshot_date", day, Observed.static, art, f"png:{field}")


def _from_json_text(em: _Emitter, art: Artefact, data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    if len(text) <= MAX_BODY and text.lstrip()[:1] in "{[":
        try:
            em.walk(json.loads(text), Observed.static, art, "$")
        except (ValueError, RecursionError):
            pass
    em.paths(text[:MAX_BODY], Observed.static, art, "text")


def extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]:
    em = _Emitter(capture)
    if capture.captured_on:
        em.add("capture_day", capture.captured_on, Observed.static, None, "capture.captured_on")
    for art in capture.artefacts:
        if not art.path:
            continue
        path = Path(root) / art.path
        if art.type in (ArtefactType.screenshot, ArtefactType.ad_screenshot):
            _from_screenshot(em, art, path)
            continue
        if art.type not in (ArtefactType.har, ArtefactType.html, ArtefactType.mhtml, ArtefactType.text, ArtefactType.other):
            continue
        data = _read(path)
        if data is None:
            continue
        try:
            if art.type is ArtefactType.har:
                _from_har(em, art, data)
            elif art.type is ArtefactType.html:
                _from_html(em, art, data)
            elif art.type is ArtefactType.mhtml:
                _from_mhtml(em, art, data)
            elif path.suffix.lower() in (".json", ".txt", ".har", ".html", ".htm"):
                _from_json_text(em, art, data)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError, UnicodeError):
            continue  # a malformed hostile file must not stop the run; what was found so far is kept
    return em.out
