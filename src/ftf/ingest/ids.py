"""Deterministic, readable ids for incidents, captures and artefacts.

Other projects key on these ids (the fetcher, ground truth, the demo), so they must not change
between runs. The separator is ``:``. Every part passes :func:`sanitise`.

| Thing | Id | Example |
|---|---|---|
| Organiser incident | ``<source>:<case folder>`` | ``do_udostepnienia:61``, ``BACKUP:p15`` |
| Organiser capture | ``<incident id>:<capture key>``; ``c1`` when the folder is one capture | ``do_udostepnienia:61:strona_1`` |
| Artefact | ``<capture id>:<path relative to the case folder>`` (``/`` kept) | ``do_udostepnienia:61:strona_1:strona_1/page.html`` |
| Fetched page | ``fetch:<fetcher capture_id>``, ``…:c1``, artefact ``…:<artefact type>`` | ``fetch:20261121T220000Z-example.test-1a2b3c4d:c1`` |
| Ad-library record | ``adlib:<ad archive id>``, captures ``…:ad`` and ``…:lander`` | ``adlib:1234567890:ad`` |
| Ad-hoc input | ``input:<first 12 hex of sha256 over the sorted artefact sha256s, or of the URL>`` | ``input:3f9a0c1b2d4e:c1`` |

Exact duplicates keep their own id and point ``duplicate_of`` at :func:`canonical` of the group.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

SEP = ":"
SINGLE_CAPTURE_KEY = "c1"

# Letters that do not decompose into ASCII + combining marks under NFKD.
_FOLD = str.maketrans(
    {
        "ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
        "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "œ": "oe", "Œ": "OE", "ı": "i", "ħ": "h", "Ħ": "H",
    }
)
_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
_DASHES = re.compile(r"-{2,}")


def sanitise(part: str) -> str:
    """NFC-normalise, fold to ASCII, map other characters to ``-``, collapse and strip dashes. Case is kept."""
    text = unicodedata.normalize("NFC", str(part)).translate(_FOLD)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = _BAD.sub("-", text)
    text = _DASHES.sub("-", text)
    return text.strip("-")


def _join(*parts: str) -> str:
    return SEP.join(parts)


def _need(part: str, what: str) -> str:
    clean = sanitise(part)
    if not clean:
        raise ValueError(f"{what} {part!r} is empty after sanitising")
    return clean


def incident_id(source: str, ref: str) -> str:
    return _join(_need(source, "source"), _need(ref, "incident ref"))


def capture_id(incident_id: str, key: str) -> str:
    return _join(incident_id, _need(key, "capture key"))


def artefact_id(capture_id: str, relpath: str) -> str:
    """``relpath`` is relative to the case folder; ``/`` separates kept segments."""
    segments = [sanitise(s) or "x" for s in str(relpath).replace("\\", "/").split("/") if s not in ("", ".")]
    if not segments:
        raise ValueError(f"artefact path {relpath!r} is empty")
    return _join(capture_id, "/".join(segments))


def fetch_incident_id(fetch_id: str) -> str:
    return _join("fetch", _need(fetch_id, "fetch id"))


def adlib_incident_id(ad_archive_id: str) -> str:
    return _join("adlib", _need(ad_archive_id, "ad archive id"))


def digest12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def input_incident_id(digests_or_url: Iterable[str] | str) -> str:
    """``input:<sha12>`` over a URL, or over the sorted artefact sha256 digests (one per line)."""
    if isinstance(digests_or_url, str):
        return _join("input", digest12(digests_or_url))
    return _join("input", digest12("\n".join(sorted(digests_or_url))))


def canonical(capture_ids: Iterable[str]) -> str:
    """The canonical capture among exact duplicates: the lexicographically smallest id."""
    ids = list(capture_ids)
    if not ids:
        raise ValueError("canonical() needs at least one capture id")
    return min(ids)
