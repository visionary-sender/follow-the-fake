"""URL lists: one http(s) URL per line, read as inert text (HACK-62).

A list such as the organiser's ``scam_links*.txt`` has a header and blank lines besides its URLs;
only lines that are one http(s) URL count. Each URL becomes its own ``url`` artefact that names
the shared list file and carries the URL's sha256 (the same digest the bare-URL path uses).
Readers pick their own line back out with :func:`find_line`, so an incident never sees the
URLs of its neighbours.
"""

from __future__ import annotations

import hashlib

BOM = b"\xef\xbb\xbf"
_SCHEMES = ("http://", "https://")


def url_line(raw: bytes) -> str | None:
    """The URL on one line of a list, or None for a line that is not one http(s) URL."""
    text = raw.strip().removeprefix(BOM).strip()
    try:
        line = text.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not line.startswith(_SCHEMES) or any(ch.isspace() for ch in line):
        return None
    return line


def digest(url: str) -> str:
    """sha256 of the URL's UTF-8 bytes: the artefact digest of a URL, as the bare-URL path computes it."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def find_line(data: bytes, wanted: str) -> str | None:
    """The one URL line of ``data`` whose :func:`digest` is ``wanted``, or None."""
    for raw in data.split(b"\n"):
        url = url_line(raw)
        if url is not None and digest(url) == wanted:
            return url
    return None


def urls(data: bytes) -> list[str]:
    """Every URL line of ``data`` in file order, duplicates kept."""
    return [u for u in (url_line(raw) for raw in data.split(b"\n")) if u is not None]
