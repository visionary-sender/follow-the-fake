"""E0: units of evaluation, and the private ground-truth spec.

The unit is a capture (one visit or saved item), not a case folder. Capture ids are taken exactly
as Ingestion writes them (`ftf.ingest.ids`); exact duplicates point `duplicate_of` at their
canonical capture and are scored as that capture.

The analyst verdicts (template and kit groups, must-not-link pairs) and the expected E1 groups
name organiser captures, so they are data, not code: `GroundTruthSpec` is read from a JSON file
kept next to the private data (default under /data/derived/evaluation, never in git).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ftf.contract import Dataset

SPEC_SCHEMA = "ftf-eval-spec@0.2"
DEFAULT_SPEC = Path(os.environ.get("FTF_EVAL_SPEC") or "/data/derived/evaluation/ground-truth-spec/spec.json")


@dataclass(frozen=True)
class GroundTruthSpec:
    """Organiser-specific evaluation facts keyed by capture id (see docs/evaluation.md)."""

    analyst_groups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    must_not_link: list[dict[str, Any]] = field(default_factory=list)
    expected_e1: dict[str, list[list[str]]] = field(default_factory=dict)
    # incidents whose captures the E1 expectations name as one unit (the incident id)
    collapse_incidents: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GroundTruthSpec":
        if data.get("schema") != SPEC_SCHEMA:
            raise ValueError(f"ground-truth spec schema {data.get('schema')!r} is not {SPEC_SCHEMA}")
        known = set(cls.__dataclass_fields__)
        extra = set(data) - known - {"schema", "note"}
        if extra:
            raise ValueError(f"unknown ground-truth spec fields: {sorted(extra)}")
        return cls(**{k: v for k, v in data.items() if k in known})


def load_spec(path: str | os.PathLike | None = None) -> GroundTruthSpec:
    """Read the spec from `path`, else $FTF_EVAL_SPEC, else the default under /data/derived/evaluation."""
    file = Path(path) if path is not None else DEFAULT_SPEC
    if not file.is_file():
        raise FileNotFoundError(
            f"ground-truth spec not found: {file}; on the VM: gcloud storage rsync -r "
            f"gs://{os.environ.get('FTF_BUCKET', '<FTF_BUCKET>')}/outputs/evaluation/ground-truth-spec/ /data/derived/evaluation/ground-truth-spec/"
        )
    return GroundTruthSpec.from_dict(json.loads(file.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class SourceKey:
    """Where a capture comes from in the organiser's material; stable across id schemes."""

    source: str  # organiser set, e.g. do_udostepnienia | BACKUP
    folder: str  # case folder inside the set (Incident.source_ref)
    key: str  # capture anchor inside the folder

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "folder": self.folder, "key": self.key}


def source_keys(ds: Dataset) -> dict[str, SourceKey]:
    """Capture id -> source key, read from the dataset (Incident.source/source_ref + capture key)."""
    out = {}
    for inc in ds.incidents:
        for cap in inc.captures:
            key = cap.id[len(inc.id) + 1 :] if cap.id.startswith(inc.id + ":") else cap.id
            out[cap.id] = SourceKey(inc.source, inc.source_ref or inc.id, key)
    return out


def canonical(ds: Dataset) -> dict[str, str]:
    """Capture id -> canonical capture id (duplicates collapse onto the capture they copy)."""
    return {c.id: (c.duplicate_of or c.id) for inc in ds.incidents for c in inc.captures}


def incident_of(ds: Dataset) -> dict[str, str]:
    """Capture id -> incident id."""
    return {c.id: inc.id for inc in ds.incidents for c in inc.captures}
