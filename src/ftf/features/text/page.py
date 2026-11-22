"""Visible text of a saved page, parsed as inert markup (never executed or rendered).

Handles saved HTML, MHTML (first text/html part) and HAR (HTML response bodies; the one with the
most visible text is taken as the main document). Ingestion owns those formats; this
keeps to what text needs and should switch to Ingestion's main-document choice once it exists.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import json
import re
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024  # HTML / MHTML: only this much is read
MAX_HAR_BYTES = 64 * 1024 * 1024  # larger HARs are skipped (parsed whole; real ones are ~5 MB)
MAX_CHARS = 50_000

_DROP = ["script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "head", "canvas"]
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "figcaption",
    "figure", "form", "label", "button", "dd", "dt", "hr", "pre",
}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _hidden(tag) -> bool:
    a = tag.attrs or {}
    if "hidden" in a or str(a.get("aria-hidden", "")).lower() == "true":
        return True
    if tag.name == "input" and str(a.get("type", "")).lower() == "hidden":
        return True
    return bool(_HIDDEN_STYLE.search(str(a.get("style", ""))))


def visible_text(html: bytes | str) -> str:
    """Text a reader would see: no script/style/hidden elements, one line per block."""
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html[:MAX_BYTES], "lxml")
    for t in soup(_DROP):
        t.decompose()
    for t in soup.find_all(_hidden):
        if not t.decomposed:
            t.decompose()
    for t in soup.find_all(_BLOCK):
        t.insert_before(NavigableString("\n"))
        t.append(NavigableString("\n"))
    text = soup.get_text("")
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)[:MAX_CHARS]


def html_from_mhtml(data: bytes) -> str | None:
    msg = email.message_from_bytes(data[:MAX_BYTES], policy=email.policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                return part.get_content()
            except (LookupError, ValueError):
                payload = part.get_payload(decode=True) or b""
                return payload.decode("utf-8", "replace")
    return None


def htmls_from_har(data: bytes) -> list[str]:
    """HTML response bodies recorded in a HAR, in entry order."""
    try:
        har = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return []
    out = []
    entries = (har.get("log") or {}).get("entries") or [] if isinstance(har, dict) else []
    for e in entries if isinstance(entries, list) else []:
        r = e.get("response") if isinstance(e, dict) else None
        c = r.get("content") if isinstance(r, dict) else None
        if not isinstance(c, dict) or not isinstance(c.get("text"), str):
            continue
        if not str(c.get("mimeType", "")).lower().startswith("text/html") or not c.get("text"):
            continue
        body = c["text"]
        if str(c.get("encoding", "")).lower() == "base64":
            try:
                body = base64.b64decode(body).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                continue
        out.append(body)
    return out


def page_text(path: Path, kind: str) -> str:
    """Visible text of an ``html`` / ``mhtml`` / ``har`` artefact ('' if none)."""
    path = Path(path)
    if kind in ("html", "mhtml"):
        with path.open("rb") as fh:
            data = fh.read(MAX_BYTES)
        if kind == "html":
            return visible_text(data)
        html = html_from_mhtml(data)
        return visible_text(html) if html else ""
    if kind == "har":
        if path.stat().st_size > MAX_HAR_BYTES:
            raise ValueError(f"HAR larger than {MAX_HAR_BYTES} bytes, skipped")
        data = path.read_bytes()
        texts = [visible_text(h) for h in htmls_from_har(data)]
        return max(texts, key=len, default="")
    raise ValueError(kind)
