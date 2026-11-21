"""Read a HAR file as inert JSON: one `Entry` per request, bodies decoded to text when textual."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_TEXT_MIME = ("text/", "javascript", "json", "xml", "x-www-form-urlencoded")


@dataclass(frozen=True)
class Entry:
    index: int
    url: str
    method: str
    post_text: str
    post_mime: str
    location: str
    body: str  # response body when textual, else ""
    body_mime: str


def _textual(mime: str) -> bool:
    m = (mime or "").lower()
    return any(t in m for t in _TEXT_MIME)


def _body(content: dict) -> str:
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return ""
    if not _textual(content.get("mimeType", "")):
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=False).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
    return text


def entries(path: Path) -> Iterator[Entry]:
    try:
        doc = json.loads(path.read_bytes().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return
    log = doc.get("log") if isinstance(doc, dict) else None
    items = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(items, list):
        return
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            continue
        req = e.get("request")
        resp = e.get("response")
        if not isinstance(req, dict):
            continue
        if not isinstance(resp, dict):
            resp = {}
        url = req.get("url") or ""
        if not isinstance(url, str) or url.startswith(("data:", "blob:", "chrome", "about:")):
            continue
        post = req.get("postData")
        if not isinstance(post, dict):
            post = {}
        raw_headers = resp.get("headers")
        if not isinstance(raw_headers, list):
            raw_headers = []
        headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in raw_headers if isinstance(h, dict)}
        content = resp.get("content") or {}
        yield Entry(
            index=i,
            url=url,
            method=str(req.get("method") or "GET").upper(),
            post_text=post.get("text") if isinstance(post.get("text"), str) else "",
            post_mime=str(post.get("mimeType") or ""),
            location=headers.get("location") or str(resp.get("redirectURL") or ""),
            body=_body(content) if isinstance(content, dict) else "",
            body_mime=str(content.get("mimeType") or "") if isinstance(content, dict) else "",
        )
