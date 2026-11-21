"""Data contract v0: the shapes every stage (extraction, linking, demo) exchanges.

Unit of analysis is the capture (one visit or one saved item), not the incident folder.
Links and campaigns therefore point at capture ids; incidents group captures.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "0.1.0"

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]*$"
_KIND = r"^[a-z][a-z0-9_]*$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Layer(str, Enum):
    """What aspect of the scam an indicator describes."""

    creative = "creative"
    distribution_account = "distribution_account"
    tracking_account = "tracking_account"
    tracker_instance = "tracker_instance"
    kit = "kit"
    platform = "platform"
    tenant = "tenant"
    owner = "owner"
    landing = "landing"
    offer = "offer"
    infrastructure = "infrastructure"
    collector = "collector"


class Level(str, Enum):
    """Granularity of the entity a value identifies (coarse to fine)."""

    ecosystem = "ecosystem"
    platform = "platform"
    tenant = "tenant"
    operator = "operator"
    campaign = "campaign"
    landing = "landing"
    capture = "capture"


class Observed(str, Enum):
    live = "live"  # seen in recorded network traffic (HAR, sandbox fetch)
    static = "static"  # seen in saved files (HTML, screenshot, text, metadata)


class ArtefactType(str, Enum):
    ad_screenshot = "ad_screenshot"
    screenshot = "screenshot"
    html = "html"
    har = "har"
    mhtml = "mhtml"
    video = "video"
    url = "url"
    text = "text"
    asset = "asset"
    ad_library = "ad_library"
    other = "other"


class Stage(str, Enum):
    ad = "ad"
    pre_lander = "pre_lander"
    lander = "lander"
    decoy = "decoy"
    unknown = "unknown"


class Relation(str, Enum):
    template = "template"
    tracking_account = "tracking_account"
    operator = "operator"
    campaign = "campaign"
    ecosystem = "ecosystem"


class LinkLevel(str, Enum):
    strict = "strict"
    medium = "medium"
    loose = "loose"


class EvidenceStatus(str, Enum):
    shared = "shared"
    conflict = "conflict"
    missing = "missing"
    hub_subsumed = "hub_subsumed"
    collector_excluded = "collector_excluded"


class Decision(str, Enum):
    assign = "assign"
    new = "new"
    review = "review"


# Indicator kinds that describe the capturing analyst or tool, never the scam. Any indicator of
# these kinds must sit in the collector layer, and the collector layer never links.
COLLECTOR_KINDS = frozenset(
    {
        "aio_fingerprint",
        "browser_fingerprint",
        "analyst_ip",
        "analyst_geo",
        "user_agent",
        "viewport",
        "screen_size",
        "fbclid_session",
        "local_path",
        "os_username",
        "capture_time",
        "capture_day",
        "screenshot_date",
    }
)


def linkable(indicator: "Indicator") -> bool:
    """True if the indicator may contribute evidence to a link."""
    return indicator.layer is not Layer.collector


def _check_version(v: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v):
        raise ValueError(f"contract_version {v!r} is not MAJOR.MINOR.PATCH")
    parts = [int(p) for p in v.split(".")]
    ours = [int(p) for p in CONTRACT_VERSION.split(".")]
    # 0.x: a minor bump is breaking. From 1.0 on only the major must match.
    want = 2 if ours[0] == 0 else 1
    if parts[:want] != ours[:want]:
        raise ValueError(f"contract_version {v} is incompatible with {CONTRACT_VERSION}")
    return v


class Artefact(_Model):
    id: str = Field(pattern=_ID)
    type: ArtefactType
    path: Optional[str] = None  # relative to the raw data root, as stored on disk
    sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: Optional[int] = Field(default=None, ge=0)


class MissingModality(_Model):
    modality: ArtefactType
    reason: str


class Capture(_Model):
    id: str = Field(pattern=_ID)
    stage: Stage
    observed: Observed = Observed.static
    captured_on: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    artefacts: list[Artefact] = Field(default_factory=list)
    missing: list[MissingModality] = Field(default_factory=list)
    duplicate_of: Optional[str] = None  # exact-duplicate captures are collapsed onto this id

    @model_validator(mode="after")
    def _missing_not_present(self) -> "Capture":
        present = {a.type for a in self.artefacts}
        clash = present & {m.modality for m in self.missing}
        if clash:
            raise ValueError(f"capture {self.id}: modality both present and missing: {sorted(c.value for c in clash)}")
        if self.duplicate_of == self.id:
            raise ValueError(f"capture {self.id} is a duplicate of itself")
        return self

    @property
    def modalities(self) -> set[ArtefactType]:
        return {a.type for a in self.artefacts}


class VectorRef(_Model):
    """Pointer to an embedding stored outside the document (no inline floats)."""

    store: str  # e.g. a relative path to a vector file
    key: str  # row key within the store
    model: str
    dim: int = Field(gt=0)


class Provenance(_Model):
    artefact_id: Optional[str] = None
    locator: Optional[str] = None  # e.g. HAR entry index, JSON path, CSS selector
    note: Optional[str] = None


class Indicator(_Model):
    id: str = Field(pattern=_ID)
    capture_id: str
    layer: Layer
    kind: str = Field(pattern=_KIND)
    level: Level
    value: Optional[str] = None
    vector_ref: Optional[VectorRef] = None
    observed: Observed
    provenance: Provenance = Field(default_factory=Provenance)
    extractor: str  # "name@version"

    @model_validator(mode="after")
    def _shape(self) -> "Indicator":
        if (self.value is None) == (self.vector_ref is None):
            raise ValueError(f"indicator {self.id}: exactly one of value / vector_ref must be set")
        if self.kind in COLLECTOR_KINDS and self.layer is not Layer.collector:
            raise ValueError(f"indicator {self.id}: kind {self.kind!r} is a collector kind, layer must be 'collector'")
        return self


class Incident(_Model):
    """One submitted item (an ad report) with its captures and extracted indicators."""

    id: str = Field(pattern=_ID)
    source: str  # e.g. "do_udostepnienia", "BACKUP", "submitted"
    source_ref: Optional[str] = None  # folder or submission id inside the source
    captures: list[Capture] = Field(min_length=1)
    indicators: list[Indicator] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> "Incident":
        cap_ids = {c.id for c in self.captures}
        art_ids = {a.id for c in self.captures for a in c.artefacts}
        for ind in self.indicators:
            if ind.capture_id not in cap_ids:
                raise ValueError(f"indicator {ind.id}: capture {ind.capture_id} not in incident {self.id}")
            aid = ind.provenance.artefact_id
            if aid is not None and aid not in art_ids:
                raise ValueError(f"indicator {ind.id}: artefact {aid} not in incident {self.id}")
        return self


class EvidenceRow(_Model):
    """One itemised line of a link's evidence ledger."""

    layer: Layer
    kind: str = Field(pattern=_KIND)
    status: EvidenceStatus
    value: Optional[str] = None  # display value, may be truncated
    bits: float = 0.0
    seen_in: Optional[str] = None  # e.g. "2 operators / 25"
    indicator_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None

    @model_validator(mode="after")
    def _collector(self) -> "EvidenceRow":
        is_collector = self.layer is Layer.collector or self.kind in COLLECTOR_KINDS
        if is_collector and self.status is not EvidenceStatus.collector_excluded:
            raise ValueError(f"evidence {self.kind}: collector evidence must have status 'collector_excluded'")
        if self.status is EvidenceStatus.collector_excluded and not is_collector:
            raise ValueError(f"evidence {self.kind}: 'collector_excluded' used on a non-collector layer")
        if self.status in (EvidenceStatus.collector_excluded, EvidenceStatus.hub_subsumed, EvidenceStatus.missing) and self.bits != 0:
            raise ValueError(f"evidence {self.kind}: status {self.status.value} must carry 0 bits")
        return self


class Link(_Model):
    id: str = Field(pattern=_ID)
    a: str  # capture id
    b: str  # capture id
    relation: Relation
    level: LinkLevel
    score_bits: Optional[float] = None
    method: str  # who/what produced it, e.g. "ledger@0.1" or "hand-labelled"
    evidence: list[EvidenceRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _endpoints(self) -> "Link":
        if self.a == self.b:
            raise ValueError(f"link {self.id}: endpoints must differ")
        return self


class Campaign(_Model):
    id: str = Field(pattern=_ID)
    label: str
    relation: Relation
    capture_ids: list[str] = Field(min_length=1)
    link_ids: list[str] = Field(default_factory=list)


class Candidate(_Model):
    campaign_id: str
    score_bits: float
    note: Optional[str] = None


class Assignment(_Model):
    incident_id: str
    candidates: list[Candidate] = Field(default_factory=list)
    decision: Decision
    campaign_id: Optional[str] = None
    evidence_capacity_bits: float = Field(ge=0)  # most bits the item could earn given its modalities
    method: str
    note: Optional[str] = None

    @model_validator(mode="after")
    def _decision(self) -> "Assignment":
        if self.decision is Decision.assign:
            if self.campaign_id is None or self.campaign_id not in {c.campaign_id for c in self.candidates}:
                raise ValueError(f"assignment {self.incident_id}: 'assign' needs campaign_id among candidates")
        elif self.campaign_id is not None:
            raise ValueError(f"assignment {self.incident_id}: campaign_id only allowed with 'assign'")
        return self


class Dataset(_Model):
    """Top-level serialised document: incidents plus (optionally) linking output."""

    contract_version: str = CONTRACT_VERSION
    name: str
    description: Optional[str] = None
    incidents: list[Incident] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    campaigns: list[Campaign] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)

    @field_validator("contract_version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _check_version(v)

    @model_validator(mode="after")
    def _refs(self) -> "Dataset":
        def unique(ids: list[str], what: str) -> set[str]:
            seen: set[str] = set()
            for i in ids:
                if i in seen:
                    raise ValueError(f"duplicate {what} id {i}")
                seen.add(i)
            return seen

        inc_ids = unique([i.id for i in self.incidents], "incident")
        cap_ids = unique([c.id for i in self.incidents for c in i.captures], "capture")
        unique([a.id for i in self.incidents for c in i.captures for a in c.artefacts], "artefact")
        indicators = {ind.id: ind for i in self.incidents for ind in i.indicators}
        unique([ind.id for i in self.incidents for ind in i.indicators], "indicator")
        link_ids = unique([l.id for l in self.links], "link")
        camp_ids = unique([c.id for c in self.campaigns], "campaign")

        captures = {c.id: c for i in self.incidents for c in i.captures}
        incident_of = {c.id: i.id for i in self.incidents for c in i.captures}
        for c in captures.values():
            dup = c.duplicate_of
            if dup is not None:
                if dup not in captures:
                    raise ValueError(f"capture {c.id}: duplicate_of unknown capture {dup}")
                if captures[dup].duplicate_of is not None:
                    raise ValueError(f"capture {c.id}: duplicate_of must point at a canonical capture, not {dup}")

        for l in self.links:
            for end in (l.a, l.b):
                if end not in cap_ids:
                    raise ValueError(f"link {l.id}: unknown capture {end}")
            ends = {incident_of[l.a], incident_of[l.b]}
            for row in l.evidence:
                for iid in row.indicator_ids:
                    ind = indicators.get(iid)
                    if ind is None:
                        raise ValueError(f"link {l.id}: unknown indicator {iid}")
                    if incident_of[ind.capture_id] not in ends:
                        raise ValueError(f"link {l.id}: indicator {iid} is not from either linked incident")
                    if not linkable(ind) and row.status is not EvidenceStatus.collector_excluded:
                        raise ValueError(f"link {l.id}: collector indicator {iid} used as evidence")
                    if ind.kind != row.kind:
                        raise ValueError(f"link {l.id}: indicator {iid} kind {ind.kind} != row kind {row.kind}")
                    if ind.layer is not row.layer:
                        raise ValueError(f"link {l.id}: indicator {iid} layer {ind.layer.value} != row layer {row.layer.value}")
        for c in self.campaigns:
            for cid in c.capture_ids:
                if cid not in cap_ids:
                    raise ValueError(f"campaign {c.id}: unknown capture {cid}")
            for lid in c.link_ids:
                if lid not in link_ids:
                    raise ValueError(f"campaign {c.id}: unknown link {lid}")
        for a in self.assignments:
            if a.incident_id not in inc_ids:
                raise ValueError(f"assignment: unknown incident {a.incident_id}")
            for cand in a.candidates:
                if cand.campaign_id not in camp_ids:
                    raise ValueError(f"assignment {a.incident_id}: unknown campaign {cand.campaign_id}")
        return self

    def incident_of(self, capture_id: str) -> Incident:
        for i in self.incidents:
            if any(c.id == capture_id for c in i.captures):
                return i
        raise KeyError(capture_id)

    def indicators(self) -> list[Indicator]:
        return [ind for i in self.incidents for ind in i.indicators]

    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(**{"indent": 2, **kwargs})

    @classmethod
    def from_json(cls, data: str | bytes) -> "Dataset":
        return cls.model_validate_json(data)
