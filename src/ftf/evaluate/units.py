"""E0: units of evaluation. The unit is a capture (one visit or saved item), not a case folder.

Prototype node names (`signals.json` keys such as "2/healthlinkq", "61/s1", "p2") map to capture
ids here and nowhere else. Ids follow Ingestion's scheme (HACK-9, `ftf.ingest.ids`):

- incident `<source>:<case folder>`, source `do_udostepnienia` or `BACKUP`;
- capture `<incident>:<key>`, key = the capture's anchor in the case folder (subfolder name, else
  its primary file's stem), `c1` when the whole folder is one capture;
- exact duplicates keep their id and point `duplicate_of` at the smallest identical id.

`ftf.ingest.ids` is imported when it is installed; until then `sanitise`, `incident_id` and
`capture_id` below follow the posted rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ftf.contract import Dataset, Stage

SOURCE_DO = "do_udostepnienia"
SOURCE_BACKUP = "BACKUP"

try:  # pragma: no cover - depends on the HACK-9 merge
    from ftf.ingest.ids import capture_id, incident_id, sanitise  # type: ignore[import-not-found]
except ImportError:

    def sanitise(part: str) -> str:
        text = unicodedata.normalize("NFC", part)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)
        return re.sub(r"-{2,}", "-", text).strip("-")

    def incident_id(source: str, ref: str) -> str:
        return f"{sanitise(source)}:{sanitise(ref)}"

    def capture_id(incident: str, key: str) -> str:
        return f"{incident}:{sanitise(key)}"


# Prototype node -> capture key inside the case folder, from the folder layout on the VM
# (/data/raw, listing only). Nodes not listed are whole-folder captures (`c1`).
_NODE_KEYS = {
    "2/acrosscommunities": "acrosscommunities.live",  # acrosscommunities.live.har
    "2/healthlinkq": "healthlinkq.pro",  # healthlinkq.pro.har (the visit belongs to case 3's chain)
    "61/ad": "reklama_1",  # reklama_1.png
    "61/s1": "strona_1",
    "61/s2": "strona_2",
    "61/s3": "strona_3",
}
_STAGE_OVERRIDES = {"61/ad": Stage.ad, "5": Stage.ad, "61/s3": Stage.decoy, "36": Stage.decoy}
# Exact duplicate nodes (E0: p1 = p2); the canonical capture is the smaller id.
DUPLICATE_GROUPS = [("p1", "p2")]


@dataclass(frozen=True)
class SourceKey:
    """Where a capture comes from in the organiser's material; stable across id schemes."""

    source: str  # organiser set: do_udostepnienia | BACKUP
    folder: str  # case folder inside the set
    key: str  # capture anchor inside the folder: c1, strona_1, healthlinkq.pro, ...

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "folder": self.folder, "key": self.key}


def node_source_key(node: str) -> SourceKey:
    """Source key of a prototype node name ("1", "2/healthlinkq", "61/s1", "p7")."""
    folder = node.split("/", 1)[0]
    source = SOURCE_BACKUP if folder.startswith("p") else SOURCE_DO
    return SourceKey(source, folder, _NODE_KEYS.get(node, "c1"))


def node_incident(node: str) -> str:
    key = node_source_key(node)
    return incident_id(key.source, key.folder)


def node_to_capture(node: str) -> str:
    """The one mapping from prototype node names to capture ids."""
    key = node_source_key(node)
    return capture_id(incident_id(key.source, key.folder), key.key)


def node_stage(node: str) -> Stage:
    return _STAGE_OVERRIDES.get(node, Stage.lander)


def duplicate_of(node: str) -> str | None:
    """Canonical capture id for a duplicate node, None for a canonical or unique one."""
    for group in DUPLICATE_GROUPS:
        if node in group:
            canon = min(node_to_capture(n) for n in group)
            return None if canon == node_to_capture(node) else canon
    return None


def source_keys(ds: Dataset) -> dict[str, SourceKey]:
    """Capture id -> source key, read from the dataset (Incident.source/source_ref + capture key)."""
    out = {}
    for inc in ds.incidents:
        for cap in inc.captures:
            out[cap.id] = SourceKey(inc.source, inc.source_ref or inc.id, cap.id[len(inc.id) + 1 :])
    return out


def canonical(ds: Dataset) -> dict[str, str]:
    """Capture id -> canonical capture id (duplicates collapse onto the capture they copy)."""
    return {c.id: (c.duplicate_of or c.id) for inc in ds.incidents for c in inc.captures}
