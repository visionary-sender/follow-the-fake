"""Technical indicators from a yt-dlp record of a video ad (HACK-79).

A video saved with yt-dlp comes with its ``<name>.info.json`` record, stored as an ``ad_library``
artefact. The record names the page or channel that published the video and the platform's media
id. The scanner's ``ad.json`` is a different record, so no other extractor reads these ids, and a
video-only case had no indicators at all.

| kind | layer | level | observed | meaning |
|---|---|---|---|---|
| `video_uploader_id` | distribution_account | operator | static | id of the page or channel that published the video (`playlist_uploader_id`, else `uploader_id`, else `channel_id`) |
| `video_media_id` | creative | capture | static | the platform's id of the video (`id`) |

Only short identifier-like values are kept. Titles, descriptions, thumbnails, format URLs and
page URLs are text or links and are not read here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from ftf import __version__
from ftf.contract import ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.ingest.manifests import file_under

EXTRACTOR = f"ytdlp@{__version__}"
MAX_BYTES = 4 * 1024 * 1024
UPLOADER_FIELDS = ("playlist_uploader_id", "uploader_id", "channel_id")
MEDIA_FIELD = "id"
IDENT = re.compile(r"[A-Za-z0-9._:-]{1,200}")

_KINDS: dict[str, tuple[Layer, Level]] = {
    "video_uploader_id": (Layer.distribution_account, Level.operator),
    "video_media_id": (Layer.creative, Level.capture),
}


def _record(capture: Capture, root: Path) -> tuple[str, dict] | None:
    for a in capture.artefacts:
        if a.type is not ArtefactType.ad_library or not a.path:
            continue
        if not Path(a.path).name.lower().endswith(".info.json"):
            continue
        path = file_under(root, a.path)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return (a.id, data) if isinstance(data, dict) else None
    return None


def _ident(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and IDENT.fullmatch(value):
        return value
    return None


def _ind(capture: Capture, kind: str, value: str, art_id: str, locator: str) -> Indicator:
    layer, level = _KINDS[kind]
    return Indicator(id=f"{capture.id}:{kind}", capture_id=capture.id, layer=layer, kind=kind, level=level,
                     value=value, observed=Observed.static,
                     provenance=Provenance(artefact_id=art_id, locator=locator), extractor=EXTRACTOR)


def extract(incident: Incident, capture: Capture, root: Path) -> Iterator[Indicator]:
    found = _record(capture, root)
    if found is None:
        return
    art_id, data = found
    for field in UPLOADER_FIELDS:
        value = _ident(data.get(field))
        if value is not None:
            yield _ind(capture, "video_uploader_id", value, art_id, f"$.{field}")
            break
    media = _ident(data.get(MEDIA_FIELD))
    if media is not None:
        yield _ind(capture, "video_media_id", media, art_id, f"$.{MEDIA_FIELD}")
