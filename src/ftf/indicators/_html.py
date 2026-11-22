"""Inert parsing of saved pages: the aio platform config, forms, and MHTML parts.

Uses only the stdlib tokenizer (`html.parser`) and `email`; nothing is rendered or executed.
"""

from __future__ import annotations

import email
import email.policy
import json
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from urllib.parse import urlencode

from ftf.indicators._ids import Hit, clean, norm_path, norm_uuid, scan_params

# --------------------------------------------------------------------------- aio config

_AIO = re.compile(r"window\.aio\s*=\s*JSON\.parse\(\s*'((?:[^'\\]|\\.)*)'\s*\)", re.S)
_JS_ESC = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_unescape(s: str) -> str:
    def sub(m: re.Match[str]) -> str:
        e = m.group(1)
        if e[0] in "ux" and len(e) > 1:
            return chr(int(e[1:], 16))
        return _SIMPLE.get(e, e)

    out = _JS_ESC.sub(sub, s)
    # Re-pair UTF-16 surrogate escapes (emoji) and drop lone ones, so values serialise as UTF-8.
    return out.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def aio_config(text: str) -> dict | None:
    """The `window.aio = JSON.parse('...')` object of an aio landing, or None."""
    m = _AIO.search(text)
    if not m:
        return None
    try:
        cfg = json.loads(_js_unescape(m.group(1)))
    except ValueError:
        return None
    return cfg if isinstance(cfg, dict) else None


def _get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# config path -> kind. Only scam-side fields; visit fields that describe the visitor
# (ip, geo, fingerprint, browser, ja3) are never read here.
_AIO_UUIDS = {
    "campaign.owner_uuid": "aio_owner",
    "campaign.uuid": "aio_campaign",
    "landing.uuid": "aio_landing",
    "landing.lander_template_uuid": "aio_lander_template",
    "source.uuid": "aio_source",
    "visit.flow_uuid": "aio_flow",
    "visit.form_uuid": "aio_form",
    "visit.advertiser_uuid": "aio_advertiser",
}
_AIO_NAMES = {"campaign.name": "aio_campaign_name", "landing.name": "aio_landing_name"}
_AIO_URLS = ("visit.query", "visit.destination_url", "visit.initial_path", "visit.path")


def aio_hits(cfg: dict) -> Iterator[tuple[Hit, str]]:
    """(hit, JSON path) pairs from an aio config."""
    yield Hit("platform_name", "aio"), "$"
    for path, kind in _AIO_UUIDS.items():
        v = _get(cfg, path)
        if isinstance(v, str) and (u := norm_uuid(v)):
            yield Hit(kind, u), f"$.{path}"
    flows = _get(cfg, "visit.flow_uuids")
    if isinstance(flows, dict):
        flows = list(flows.values())
    elif isinstance(flows, str):  # some builds store "{0: <uuid>, 1: <uuid>}" as a string
        flows = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", flows)
    if isinstance(flows, list):
        for n, v in enumerate(flows):
            if isinstance(v, str) and (u := norm_uuid(v)):
                yield Hit("aio_flow", u), f"$.visit.flow_uuids[{n}]"
    for path, kind in _AIO_NAMES.items():
        v = _get(cfg, path)
        if isinstance(v, str) and (c := clean(v)):
            yield Hit(kind, c), f"$.{path}"
    key = _get(cfg, "handle.key")
    if isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{16,64}", key):
        yield Hit("aio_handle_key", key.lower()), "$.handle.key"
    for path in _AIO_URLS:
        v = _get(cfg, path)
        if isinstance(v, dict):
            v = "?" + urlencode({k: x for k, x in v.items() if isinstance(x, (str, int))})
        if isinstance(v, str) and v:
            if not v.startswith(("?", "&")) and "?" not in v:
                v = "?" + v
            for h in scan_params(v):
                yield h, f"$.{path}"


# --------------------------------------------------------------------------- forms

_SKIP_TYPES = {"submit", "button", "image", "reset"}


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str | None, set[str]]] = []
        self._open = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self.forms.append((a.get("action"), set()))
            self._open = True
        elif self._open and tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "").lower() in _SKIP_TYPES:
                return
            name = a.get("name", "").strip().lower()
            if name and len(name) <= 64:
                self.forms[-1][1].add(name)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = False


def form_hits(html: str) -> Iterator[tuple[Hit, str]]:
    """form_endpoint and form_field_set hits, located as `form[n]`."""
    p = _Forms()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    for n, (action, fields) in enumerate(p.forms):
        if action and (path := norm_path(action)):
            yield Hit("form_endpoint", path), f"form[{n}]@action"
        if fields:
            yield Hit("form_field_set", ",".join(sorted(fields))), f"form[{n}]"


def form_body_fields(body: str, mime: str) -> str | None:
    """Sorted field names of a url-encoded POST body, or None."""
    if "x-www-form-urlencoded" not in mime.lower():
        return None
    names = sorted({kv.split("=", 1)[0].strip().lower() for kv in body.split("&") if "=" in kv})
    names = [n for n in names if n and len(n) <= 64 and re.fullmatch(r"[a-z0-9_\-\[\].]+", n)]
    return ",".join(names) or None


# --------------------------------------------------------------------------- MHTML

def mhtml_parts(data: bytes) -> Iterator[tuple[int, str, str, str]]:
    """(part index, content type, Content-Location, text) for each textual part of an MHTML archive."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        if not (ct.startswith("text/") or "javascript" in ct or "json" in ct):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        yield i, ct, str(part.get("Content-Location") or ""), text
