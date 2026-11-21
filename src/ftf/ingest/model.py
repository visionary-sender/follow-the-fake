"""In-memory shapes the adapters fill before ids are minted and the contract Dataset is built."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ftf.contract import ArtefactType, MissingModality, Observed, Stage
from ftf.ingest import files as F

PAGE_TYPES = frozenset({ArtefactType.html, ArtefactType.har, ArtefactType.mhtml})
AD_TYPES = frozenset({ArtefactType.ad_screenshot, ArtefactType.video, ArtefactType.ad_library})


@dataclass
class RawArtefact:
    rel: str  # path relative to the case folder (or the input's own folder); used in the artefact id
    type: ArtefactType
    path: Path | None = None  # absolute; None for a bare URL
    sha256: str | None = None
    size: int | None = None
    info: F.PageInfo | None = None
    seconds: float = 0.0
    fixed_id: str | None = None  # kept as is (fetcher artefacts)


@dataclass
class RawCapture:
    key: str
    artefacts: list[RawArtefact] = field(default_factory=list)
    stage: Stage | None = None  # None: decided by the stage rules
    observed: Observed | None = None
    captured_on: str | None = None
    missing: list[MissingModality] | None = None  # None: computed from the expected modalities
    missing_reason: str = "not in the submission"
    notes: list[str] = field(default_factory=list)
    fixed_id: str | None = None
    seconds: float = 0.0  # extra load time not attributed to an artefact

    def types(self) -> set[ArtefactType]:
        return {a.type for a in self.artefacts}

    def is_page(self) -> bool:
        return bool(self.types() & PAGE_TYPES)

    def page_infos(self, *types: ArtefactType) -> list[F.PageInfo]:
        order = types or (ArtefactType.har, ArtefactType.mhtml, ArtefactType.html)
        return [a.info for t in order for a in self.artefacts if a.type is t and a.info is not None]

    @property
    def host(self) -> str | None:
        return next((i.host for i in self.page_infos() if i.host), None)

    @property
    def started(self) -> str | None:
        times = [i.started for i in self.page_infos(ArtefactType.har, ArtefactType.mhtml) if i.started]
        return min(times) if times else None

    def load_seconds(self) -> float:
        return self.seconds + sum(a.seconds for a in self.artefacts)


@dataclass
class RawIncident:
    source: str
    source_ref: str | None
    captures: list[RawCapture]
    ref: str | None = None  # organiser case folder; None for ad-hoc inputs (id from content)
    fixed_id: str | None = None
    notes: list[str] = field(default_factory=list)
