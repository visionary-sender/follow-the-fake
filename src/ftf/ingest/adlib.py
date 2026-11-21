"""Read the scanner's ``ad.json`` record of a capture, so extractors need not re-parse it.

``load_ad(capture, root)`` returns a dict with every field of the HACK-20 layout (``None`` when
absent) plus readable aliases::

    ad_text      = body              first_date = start_date
    page_name, page_id, advertiser_id, advertiser_name, landing_url, landing_domain, ...
    last_date    = end_date

It returns ``{}`` when the capture has no ``ad.json`` artefact or the file is not a JSON object.
The record is untrusted text: values are returned as stored, never fetched or rendered.
"""

from __future__ import annotations

import json
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Capture
from ftf.ingest.manifests import AD_RECORD, file_under

MAX_AD_BYTES = 4 * 1024 * 1024

FIELDS = (
    "source", "kind", "stable_id", "ad_archive_id", "ad_id", "key", "keyword", "run_id",
    "page_id", "page_name", "advertiser_id", "advertiser_name", "body", "title", "link_description",
    "display_domain", "landing_url", "declared_link_url", "landing_domain", "creative_urls",
    "collation_id", "display_format", "platforms", "start_date", "end_date", "status", "register_id",
)
ALIASES = {"ad_text": "body", "first_date": "start_date", "last_date": "end_date"}


def ad_artefact(capture: Capture) -> Artefact | None:
    """The capture's ``ad.json`` (an ``ad_library`` artefact; yt-dlp ``.info.json`` records are not it)."""
    for a in capture.artefacts:
        if a.type is ArtefactType.ad_library and a.path and Path(a.path).name == AD_RECORD:
            return a
    return None


def load_ad(capture: Capture, root: Path) -> dict:
    a = ad_artefact(capture)
    path = file_under(root, a.path) if a is not None else None
    if path is None:
        return {}
    try:
        if path.stat().st_size > MAX_AD_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: data.get(k) for k in FIELDS}
    out.update({alias: data.get(field) for alias, field in ALIASES.items()})
    return out
