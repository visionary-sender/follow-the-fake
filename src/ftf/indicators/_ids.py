"""Identifier scanners for page code and traffic text: regexes, decoders and normalisers.

Everything here works on plain strings. Nothing is executed, rendered or fetched.
`scan(text)` yields `Hit`s; callers decide `observed` and provenance. Access tokens are cut out
of the text before any pattern runs, so no value can carry one.
"""

from __future__ import annotations

import base64
import posixpath
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote_plus

# kind -> (layer, level). Layers and levels are contract enum values.
KINDS: dict[str, tuple[str, str]] = {
    "platform_name": ("platform", "platform"),
    "aio_sdk_asset": ("platform", "platform"),
    "aio_action_files": ("platform", "platform"),
    "aio_cdn_tenant": ("tenant", "tenant"),
    "aio_handle_key": ("tenant", "tenant"),
    "aio_flow": ("tenant", "campaign"),
    "aio_owner": ("owner", "operator"),
    "aio_campaign": ("owner", "campaign"),
    "aio_campaign_name": ("owner", "campaign"),
    "aio_landing": ("landing", "landing"),
    "aio_landing_name": ("landing", "landing"),
    "aio_lander_template": ("kit", "platform"),
    "aio_source": ("distribution_account", "operator"),
    "aio_form": ("offer", "campaign"),
    "aio_advertiser": ("offer", "operator"),
    "meta_pixel": ("tracking_account", "operator"),
    "capi_gateway": ("tracking_account", "operator"),
    "google_ads_id": ("tracking_account", "operator"),
    "google_analytics_id": ("tracking_account", "operator"),
    "gtm_container": ("tracking_account", "operator"),
    "tiktok_pixel": ("tracking_account", "operator"),
    "clarity_id": ("tracking_account", "operator"),
    "yandex_metrica_id": ("tracking_account", "operator"),
    "push_client_id": ("tracking_account", "operator"),
    "xid_tracker_machine": ("tracker_instance", "operator"),
    "tracker_postback": ("tracker_instance", "operator"),
    "meta_campaign_id": ("distribution_account", "campaign"),
    "meta_adset_id": ("distribution_account", "campaign"),
    "meta_campaign_name": ("distribution_account", "campaign"),
    "meta_adset_name": ("distribution_account", "campaign"),
    "meta_ad_id": ("creative", "campaign"),
    "meta_ad_name": ("creative", "campaign"),
    "offer_id": ("offer", "campaign"),
    "form_endpoint": ("offer", "platform"),
    "form_field_set": ("offer", "platform"),
    "contact_phone": ("offer", "operator"),
    "contact_email": ("offer", "operator"),
    "contact_telegram": ("offer", "operator"),
    "video_cdn_tenant": ("infrastructure", "operator"),
}


@dataclass(frozen=True)
class Hit:
    kind: str
    value: str
    note: str | None = None


# --------------------------------------------------------------------------- access tokens

# Same separators and value end as `ftf.indicators.redact`, for any `*token` key (also in a URL
# fragment), plus bare EAA tokens after any %xx or \u00xx escape.
_TOKEN_SEP = r"(?:=|%3D|%253D|\\u003d|\\?\"\s*:\s*\\?\"|%22%3A%22|%2522%253A%2522)"
_TOKEN_VALUE = r"(?:(?!%2[0236]|%25|\\u00)[^&\s\"'<>#,;}\\])*"
_TOKEN_PARAM = re.compile(
    r"((?:^|[?&;#\s\"'\\]|%26|%3F|%23|%22|%2526|%253F)(?:access_token|[a-z_]*token)" + _TOKEN_SEP + ")" + _TOKEN_VALUE, re.I
)
_EAA = re.compile(r"(?:(?<=%[0-9A-Fa-f]{2})|(?<=\\u00[0-9A-Fa-f]{2})|(?<![A-Za-z0-9]))EAA[A-Za-z0-9]{10,}")


def strip_tokens(text: str) -> str:
    """Cut every `*token=` parameter value and every Meta `EAA...` token out of `text`."""
    text = _TOKEN_PARAM.sub(lambda m: m.group(1), text)
    return _EAA.sub("", text)


# --------------------------------------------------------------------------- normalisers

def clean(value: str, limit: int = 200) -> str:
    v = unicodedata.normalize("NFC", strip_tokens(value))
    v = re.sub(r"\s+", " ", v).strip()
    return v[:limit]


def norm_uuid(v: str) -> str | None:
    v = v.strip().lower()
    return v if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v) else None


# Obvious fillers plus the libphonenumber / intl-tel-input example numbers (PL, CZ, SK, DE, UK, ...).
_PLACEHOLDER_NATIONAL = {
    "123456789", "987654321", "111222333", "123123123", "500600700", "000000000",
    "512345678", "601123456", "912123456", "15123456789", "7400123456", "201555012",
}


def norm_phone(raw: str) -> str | None:
    """E.164 (`+<digits>`). Bare 9-digit numbers are read as Polish. Placeholders are dropped."""
    s = raw.strip()
    plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if not plus:
        if len(digits) == 9:
            digits = "48" + digits
        elif not (len(digits) == 11 and digits.startswith("48")):
            return None
    if not 8 <= len(digits) <= 15:
        return None
    national = digits[2:] if digits.startswith("48") else digits[-9:]
    if national in _PLACEHOLDER_NATIONAL or len(set(national)) <= 2 or re.search(r"0{5,}|x", national):
        return None
    return "+" + digits


_GENERIC_EMAIL = re.compile(
    r"(example\.|\.test$|\.invalid$|\.local$|sentry|wixpress|@2x|\.(png|jpe?g|webp|svg|gif|css|js)$"
    r"|^(user|name|email|your|test|you|someone|john\.doe)@"
    # MHTML content ids and authors of public libraries bundled into kits
    r"|@mhtml\.blink$|@(broofa\.com|feross\.org|jquery\.com|getbootstrap\.com|w3\.org|mozilla\.org|npmjs\.com)$)",
    re.I,
)


def norm_email(v: str) -> str | None:
    v = v.strip().strip(".").lower()
    if _GENERIC_EMAIL.search(v):
        return None
    return v


_UUID_SEG = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def norm_path(path: str) -> str | None:
    """Form endpoint path: no scheme/host/query, lower-case, dot segments resolved, UUIDs masked."""
    p = path.strip()
    if not p or p.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "about:")):
        return None
    p = re.sub(r"^[a-z][a-z0-9+.\-]*://[^/]*", "", p, flags=re.I)
    if p.startswith("//"):
        p = re.sub(r"^//[^/]*", "", p)
    p = unquote_plus(p).split("#", 1)[0].split("?", 1)[0]
    p = re.sub(r"/{2,}", "/", p).lower()
    trailing = p.endswith("/")
    p = posixpath.normpath("/" + p.lstrip("/"))
    if p.startswith("//"):
        p = p[1:]
    if trailing and p != "/":
        p += "/"
    # Per-visit ids in the path (aio /api/v1/trigger/field/<visit>/) would make every visit unique.
    p = _UUID_SEG.sub("{uuid}", p)
    if p in ("/", "/index.html", "/index.php"):
        return None
    return p[:200]


# --------------------------------------------------------------------------- xid click ids

_XID_EPOCH_MIN = datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp()
_XID_EPOCH_MAX = datetime(2035, 1, 1, tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class Xid:
    time: datetime
    machine: str
    pid: int
    counter: int


def decode_xid(s: str) -> Xid | None:
    """Decode a 20-char base32hex xid (4-byte time, 3-byte machine, 2-byte pid, 3-byte counter)."""
    if not re.fullmatch(r"[0-9a-v]{20}", s):
        return None
    try:
        b = base64.b32hexdecode(s.upper() + "====")
    except ValueError:
        return None
    if len(b) != 12:
        return None
    ts = int.from_bytes(b[0:4], "big")
    if not _XID_EPOCH_MIN <= ts <= _XID_EPOCH_MAX:
        return None
    return Xid(
        time=datetime.fromtimestamp(ts, tz=timezone.utc),
        machine=b[4:7].hex(),
        pid=int.from_bytes(b[7:9], "big"),
        counter=int.from_bytes(b[9:12], "big"),
    )


# --------------------------------------------------------------------------- patterns

_PIXEL = r"(\d{15,16})"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("meta_pixel", re.compile(r"""fbq\(\s*['"]init['"]\s*,\s*['"]?""" + _PIXEL + r"\b")),
    ("meta_pixel", re.compile(r"facebook\.com/tr/?\?(?:[^\"'\s<>]*?&)?id=" + _PIXEL + r"\b")),
    ("meta_pixel", re.compile(r"signals/config/" + _PIXEL + r"\b")),
    ("meta_pixel", re.compile(r"""["']?fb\.pixel_id["']?\s*[:=]\s*["']?""" + _PIXEL + r"\b")),
    ("meta_pixel", re.compile(r"""["'](?:pixel|pixelId|pixel_id|fbPixel|fb_pixel|fbpx|pixelID)["']\s*[:=]\s*["']?""" + _PIXEL + r"\b")),
    ("google_ads_id", re.compile(r"\b(AW-\d{8,12})\b")),
    ("google_analytics_id", re.compile(r"\b(G-(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,12})\b")),
    ("google_analytics_id", re.compile(r"\b(UA-\d{4,10}-\d{1,3})\b")),
    ("gtm_container", re.compile(r"\b(GTM-(?=[A-Z0-9]*\d)[A-Z0-9]{5,9})\b")),
    ("tiktok_pixel", re.compile(r"""ttq\.load\(\s*['"]([A-Z0-9]{18,22})['"]""")),
    ("clarity_id", re.compile(r"""['"]clarity['"]\s*,\s*['"]script['"]\s*,\s*['"]([a-z0-9]{8,12})['"]""")),
    ("yandex_metrica_id", re.compile(r"""\bym\(\s*(\d{6,10})\s*,\s*['"]init""")),
    ("yandex_metrica_id", re.compile(r"mc\.yandex\.ru/watch/(\d{6,10})")),
    ("push_client_id", re.compile(r"/clients/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/sdk\.js")),
    ("capi_gateway", re.compile(r"\b([a-z]{2}-[0-9a-f]{32}\.ecs\.[a-z0-9\-]+\.on\.aws)\b")),
    ("video_cdn_tenant", re.compile(r"\b(vz-[0-9a-f]{8}-[0-9a-f]{3})(?![0-9a-z\-])")),
    ("aio_cdn_tenant", re.compile(r"/_cdn/production/landing-cdn/([0-9a-f][0-9a-f\-]{2,35})/")),
    ("aio_landing", re.compile(r"/_cdn/production/landing-cdn/[0-9a-f][0-9a-f\-]{2,35}/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")),
    ("aio_action_files", re.compile(r"/_cdn/production/action-files/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")),
    ("aio_sdk_asset", re.compile(r"aio-static/sdk/(main\.(?:js|css)\?v=\d+(?:\.\d+){1,3})")),
    ("contact_telegram", re.compile(r"(?:\bt\.me|\btelegram\.me)/([A-Za-z0-9_]{5,32})\b")),
    ("contact_email", re.compile(r"(?:mailto:)?\b([A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,12})\b")),
    ("contact_phone", re.compile(r"""tel:([+\d][\d\s\-().]{6,22}\d)""")),
    ("contact_phone", re.compile(r"(?<![\w+])(\+48[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{3})(?!\d)")),
]

_PLATFORM_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("aio", re.compile(r"window\.aio\s*=|aio-static/sdk/")),
    ("binom", re.compile(r"\bBINOM_[A-Z_]+|window\.BINOM\b")),
    ("keitaro", re.compile(r"\bKTracking\b|keitaro", re.I)),
]

_TG_GENERIC = {"share", "joinchat", "addstickers", "proxy", "socks", "iv", "setlanguage", "addtheme"}

# URL parameters. Values are URL-decoded. Click-id params feed the xid decoder only.
_PARAM = re.compile(
    r"[?&;](ad_id|adset_id|campaign_id|ad_name|adset_name|campaign_name|offer_id|pixel|pxl|fbpx|fb_pixel|pixel_id"
    r"|clickid|upd_clickid|click_id|bcid)=([^&\"'\s<>#\\]*)",
    re.I,
)
_META_ID_PARAMS = {"ad_id": "meta_ad_id", "adset_id": "meta_adset_id", "campaign_id": "meta_campaign_id"}
_META_NAME_PARAMS = {"ad_name": "meta_ad_name", "adset_name": "meta_adset_name", "campaign_name": "meta_campaign_name"}
_PIXEL_PARAMS = {"pixel", "pxl", "fbpx", "fb_pixel", "pixel_id"}
_CLICK_PARAMS = {"clickid", "upd_clickid", "click_id", "bcid"}
_POSTBACK = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9.\-]*\.[a-z]{2,24})(/[A-Za-z0-9_\-./]*)\?[^\"'\s<>]*\bupd_clickid=", re.I)


def _is_macro(v: str) -> bool:
    return not v or "{" in v or "}" in v or v.startswith("%") or v.lower() in ("null", "undefined", "none", "(not set)")


def scan_params(text: str) -> Iterator[Hit]:
    for m in _PARAM.finditer(text):
        name = m.group(1).lower()
        raw = unquote_plus(m.group(2))
        if name in _META_ID_PARAMS:
            if re.fullmatch(r"\d{15,20}", raw):
                yield Hit(_META_ID_PARAMS[name], raw)
        elif name in _META_NAME_PARAMS:
            v = clean(raw)
            if not _is_macro(v) and len(v) >= 3:
                yield Hit(_META_NAME_PARAMS[name], v)
        elif name in _PIXEL_PARAMS:
            if re.fullmatch(r"\d{15,16}", raw):
                yield Hit("meta_pixel", raw)
        elif name == "offer_id":
            v = clean(raw, 64)
            if re.fullmatch(r"[A-Za-z0-9_\-]{3,64}", v):
                yield Hit("offer_id", v)
        elif name in _CLICK_PARAMS:
            x = decode_xid(raw.lower())
            if x is not None:
                yield Hit("xid_tracker_machine", x.machine, f"xid pid {x.pid}, {x.time:%Y-%m-%dT%H:%M:%SZ}")
    for m in _POSTBACK.finditer(text):
        yield Hit("tracker_postback", m.group(1).lower() + m.group(2).lower())


CONTACT_KINDS = frozenset({"contact_phone", "contact_email", "contact_telegram"})
_WHATSAPP = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send/?\?phone=)\+?(\d{8,15})")
_PLACEHOLDER_ATTR = re.compile(r"""placeholder\s*=\s*(?:"[^"]*"|'[^']*')""", re.I)


def scan(text: str, contacts: bool = True) -> Iterator[Hit]:
    """All identifier hits in `text` (page code, a URL, a request body).

    `contacts=False` skips phones, emails and messenger handles: use it on what the collector
    sent (request URLs and bodies), which can hold the analyst's own form input.
    """
    text = strip_tokens(text.replace("&amp;", "&").replace("\\/", "/").replace("\\u0026", "&"))
    for name, rx in _PLATFORM_MARKERS:
        if rx.search(text):
            yield Hit("platform_name", name)
    # Input placeholders show example numbers and addresses, not the operator's.
    contact_text = _PLACEHOLDER_ATTR.sub("", text) if contacts else ""
    for kind, rx in _PATTERNS:
        is_contact = kind in CONTACT_KINDS
        if is_contact and not contacts:
            continue
        for m in rx.finditer(contact_text if is_contact else text):
            v = m.group(1)
            if kind == "contact_phone":
                v = norm_phone(v)
            elif kind == "contact_email":
                v = norm_email(v)
            elif kind == "contact_telegram":
                v = v.lower()
                if v in _TG_GENERIC:
                    v = None
            elif kind in ("capi_gateway", "clarity_id", "video_cdn_tenant", "aio_cdn_tenant"):
                v = v.lower()
            elif kind in ("aio_landing", "aio_action_files", "push_client_id"):
                v = norm_uuid(v)
            if v:
                yield Hit(kind, v)
    if contacts:
        for m in _WHATSAPP.finditer(contact_text):
            # wa.me numbers are always international, written without the "+".
            if v := norm_phone("+" + m.group(1)):
                yield Hit("contact_phone", v)
    yield from scan_params(text)
