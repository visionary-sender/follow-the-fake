"""Image response bodies recorded in a HAR file, read as inert JSON and base64 bytes."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import Path

MAX_BODY = 25_000_000
MAX_HAR = 200_000_000  # bytes; a larger HAR is skipped rather than loaded whole  # bytes of a decoded body


def iter_images(path: Path) -> Iterator[tuple[int, str, bytes]]:
    """``(entry index, request URL, body bytes)`` for base64 image responses other than SVG."""
    try:
        if Path(path).stat().st_size > MAX_HAR:
            return
        doc = json.loads(Path(path).read_bytes())
        entries = doc["log"]["entries"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return
    if not isinstance(entries, list):
        return
    for i, e in enumerate(entries):
        try:
            content = e["response"]["content"]
            mime = str(content.get("mimeType") or "").split(";")[0].strip().lower()
            text = content.get("text")
            url = str(e["request"]["url"])
        except (KeyError, TypeError, AttributeError):
            continue
        if not mime.startswith("image/") or "svg" in mime or not isinstance(text, str):
            continue
        if content.get("encoding") != "base64" or len(text) > MAX_BODY * 4 // 3 + 4:
            continue
        try:
            body = base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError):
            continue
        if body:
            yield i, url, body
