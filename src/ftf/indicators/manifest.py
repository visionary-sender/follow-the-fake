"""Collector facts from our own fetcher's ``capture.json`` (artefact ``<capture id>:manifest``).

- ``fetch_profile``: the visitor profile the fetch used (``default``, ``victim-pl-mobile``, ...).
- ``fetch_outcome``: the fetcher's verdict, ``<outcome>: <reason>``.

Both describe our visit, not the scam: layer ``collector``, level ``capture``, never linkable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ftf import __version__
from ftf.contract import ArtefactType, Capture, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.ingest.manifests import FETCH_TOOL, MANIFEST, MAX_MANIFEST_BYTES, file_under, profile_name

EXTRACTOR = f"manifest@{__version__}"


def _manifest(capture: Capture, root: Path) -> tuple[str, dict] | None:
    for a in capture.artefacts:
        if a.type is not ArtefactType.other or not a.path or Path(a.path).name != MANIFEST:
            continue
        path = file_under(root, a.path)
        if path is None:
            continue
        try:
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return a.id, data
    return None


def extract(incident: Incident, capture: Capture, root: Path) -> Iterator[Indicator]:
    found = _manifest(capture, root)
    if found is None:
        return
    art_id, data = found
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool.startswith(FETCH_TOOL):
        return

    def ind(kind: str, value: str, locator: str) -> Indicator:
        return Indicator(id=f"{capture.id}:{kind}", capture_id=capture.id, layer=Layer.collector, kind=kind, level=Level.capture,
                         value=value, observed=Observed.static, provenance=Provenance(artefact_id=art_id, locator=locator),
                         extractor=EXTRACTOR)

    yield ind("fetch_profile", profile_name(data), "profile.name")
    outcome, reason = data.get("outcome"), data.get("reason")
    if isinstance(outcome, str) and outcome:
        yield ind("fetch_outcome", f"{outcome}: {reason}" if isinstance(reason, str) and reason else outcome, "outcome")
