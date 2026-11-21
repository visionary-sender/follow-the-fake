"""Follow the Fake data contract (see docs/contract.md)."""

from ftf.contract.models import (
    COLLECTOR_KINDS,
    CONTRACT_VERSION,
    Artefact,
    ArtefactType,
    Assignment,
    Campaign,
    Candidate,
    Capture,
    Dataset,
    Decision,
    EvidenceRow,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Level,
    Link,
    LinkLevel,
    MissingModality,
    Observed,
    Provenance,
    Relation,
    Stage,
    VectorRef,
    linkable,
)


def json_schema() -> dict:
    """JSON Schema of the top-level Dataset document (all entities under $defs)."""
    schema = Dataset.model_json_schema()
    schema["$comment"] = f"ftf data contract {CONTRACT_VERSION}"
    return schema


__all__ = [
    "COLLECTOR_KINDS",
    "CONTRACT_VERSION",
    "Artefact",
    "ArtefactType",
    "Assignment",
    "Campaign",
    "Candidate",
    "Capture",
    "Dataset",
    "Decision",
    "EvidenceRow",
    "EvidenceStatus",
    "Incident",
    "Indicator",
    "Layer",
    "Level",
    "Link",
    "LinkLevel",
    "MissingModality",
    "Observed",
    "Provenance",
    "Relation",
    "Stage",
    "VectorRef",
    "json_schema",
    "linkable",
]
