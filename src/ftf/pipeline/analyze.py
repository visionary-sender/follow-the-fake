"""`analyze()`: run the stage adapters over one input and collect a contract-valid result."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from ftf.contract import (
    CONTRACT_VERSION,
    Assignment,
    Dataset,
    Decision,
    Incident,
    Link,
)

INPUT_KINDS = ("url", "screenshot", "har", "html", "mhtml", "media", "dataset_item", "keyword")
TEXT_KINDS = frozenset({"url", "keyword"})
FILE_KINDS = frozenset({"screenshot", "har", "html", "mhtml", "media"})
StageStatus = Literal["ok", "skipped", "not_available", "error"]

_EXTENSIONS = {
    ".har": "har",
    ".html": "html",
    ".htm": "html",
    ".mhtml": "mhtml",
    ".mht": "mhtml",
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
    ".webp": "screenshot",
    ".mp4": "media",
    ".webm": "media",
    ".mov": "media",
    ".gif": "media",
}


@dataclass(frozen=True)
class AnalysisInput:
    """One submitted item. Text kinds (`url`, `keyword`) use `text`; file kinds use `path`.

    `dataset_item` takes an incident id of the base in `text`, or a case folder in `path`.
    `name` is the original file name of an upload (display only).
    """

    kind: str
    path: Optional[Path] = None
    text: Optional[str] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in INPUT_KINDS:
            raise ValueError(f"unknown input kind {self.kind!r}; expected one of {', '.join(INPUT_KINDS)}")
        if self.path is not None and not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))
        if self.kind in TEXT_KINDS and not (self.text or "").strip():
            raise ValueError(f"input kind {self.kind!r} needs text")
        if self.kind in FILE_KINDS:
            if self.path is None:
                raise ValueError(f"input kind {self.kind!r} needs a file path")
            if not self.path.is_file():
                raise ValueError(f"not a file: {self.path}")
        if self.kind == "dataset_item":
            if self.path is None and not (self.text or "").strip():
                raise ValueError("input kind 'dataset_item' needs an incident id or a case folder")
            if self.path is not None and not self.path.is_dir():
                raise ValueError(f"not a case folder: {self.path}")

    @property
    def label(self) -> str:
        if self.text:
            return self.text.strip()
        return self.name or (self.path.name if self.path else self.kind)


@dataclass(frozen=True)
class StageReport:
    name: str
    status: StageStatus
    note: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class SimilarItem:
    """A base capture linked to one of the analysed captures, ranked by `score_bits`."""

    incident_id: str
    capture_id: str
    own_capture_id: str
    score_bits: Optional[float]
    relation: str
    level: str
    link_id: str


@dataclass
class AnalysisResult:
    input: AnalysisInput
    incident: Incident
    links: list[Link]
    assignment: Assignment
    similar: list[SimilarItem]
    stages: list[StageReport]
    base: Dataset
    run_dir: Optional[Path] = None
    input_root: Optional[Path] = None  # artefact paths of `incident` are relative to this
    from_base: bool = False  # the incident is a base item; links/assignment are precomputed
    notes: list[str] = field(default_factory=list)
    enrichment: Optional[str] = None  # "online", "offline cache" or "mixed"; None if enrichment did not run

    @property
    def ok(self) -> bool:
        return all(s.status != "error" for s in self.stages)

    def to_dataset(self, name: str | None = None) -> Dataset:
        """The incident, its links and assignment, plus the base items they refer to.

        Base incidents and campaigns are included only as far as references need them
        (link endpoints, candidate campaigns and their members and links, duplicate targets),
        so the document validates as a contract `Dataset` on its own.
        """
        base = self.base
        base_incidents = {i.id: i for i in base.incidents}
        base_links = {l.id: l for l in base.links}
        base_campaigns = {c.id: c for c in base.campaigns}
        capture_owner = {c.id: i.id for i in base.incidents for c in i.captures}
        captures = {c.id: c for i in base.incidents for c in i.captures}
        own_captures = {c.id for c in self.incident.captures}

        links = {l.id: l for l in self.links}
        campaign_ids = [c.campaign_id for c in self.assignment.candidates if c.campaign_id in base_campaigns]
        campaigns = {cid: base_campaigns[cid] for cid in campaign_ids}
        for camp in campaigns.values():
            for lid in camp.link_ids:
                if lid in base_links:
                    links.setdefault(lid, base_links[lid])

        needed: set[str] = set()
        for l in links.values():
            needed.update(capture_owner[e] for e in (l.a, l.b) if e not in own_captures and e in capture_owner)
        for camp in campaigns.values():
            needed.update(capture_owner[c] for c in camp.capture_ids if c in capture_owner)
        needed.discard(self.incident.id)
        pending = list(needed)
        while pending:  # duplicate_of targets may live in further incidents
            inc = base_incidents[pending.pop()]
            for cap in inc.captures:
                dup = cap.duplicate_of
                if dup and dup in captures:
                    owner = capture_owner[dup]
                    if owner not in needed and owner != self.incident.id:
                        needed.add(owner)
                        pending.append(owner)
        for cap in self.incident.captures:
            dup = cap.duplicate_of
            if dup and dup in capture_owner and capture_owner[dup] != self.incident.id:
                needed.add(capture_owner[dup])

        incidents = [self.incident] + [i for i in base.incidents if i.id in needed]
        assignment = self.assignment
        unknown = [c.campaign_id for c in assignment.candidates if c.campaign_id not in base_campaigns]
        if unknown:  # e.g. a proposed new campaign: the document can only reference base campaigns
            kept = [c for c in assignment.candidates if c.campaign_id in base_campaigns]
            update = {"candidates": kept,
                      "note": " ".join(x for x in (assignment.note, f"candidates not in the base left out: {', '.join(unknown)}") if x)}
            if assignment.decision.value == "assign" and assignment.campaign_id in unknown:
                update.update(decision=Decision.review, campaign_id=None)
            assignment = assignment.model_copy(update=update)
        return Dataset(
            contract_version=CONTRACT_VERSION,
            name=name or f"analysis of {self.input.kind}: {self.input.label}"[:200],
            description="ftf analyze: one incident, its links and assignment, plus the base items they reference",
            incidents=incidents,
            links=list(links.values()),
            campaigns=list(campaigns.values()),
            assignments=[assignment],
        )

    def summary(self) -> dict:
        a = self.assignment
        return {
            "input": {"kind": self.input.kind, "label": self.input.label},
            "incident_id": self.incident.id,
            "from_base": self.from_base,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "stages": [s.__dict__ for s in self.stages],
            "decision": a.decision.value,
            "campaign_id": a.campaign_id,
            "candidates": [{"campaign_id": c.campaign_id, "score_bits": c.score_bits} for c in a.candidates],
            "evidence_capacity_bits": a.evidence_capacity_bits,
            "links": len(self.links),
            "indicators": len(self.incident.indicators),
            "missing": sorted({m.modality.value for c in self.incident.captures for m in c.missing}),
            "similar": [s.__dict__ for s in self.similar[:20]],
            "enrichment": self.enrichment,
            "notes": self.notes,
        }


def guess_kind(value: str, base: Dataset | None = None) -> str:
    """Input kind from a command-line value: folder, file extension, URL, base incident id, else keyword."""
    path = Path(value)
    try:
        if path.is_dir():
            return "dataset_item"
        if path.is_file():
            kind = _EXTENSIONS.get(path.suffix.lower())
            if kind is None:
                raise ValueError(f"cannot tell the input kind of {path.name}; pass --kind")
            return kind
    except OSError:
        pass
    low = value.strip().lower()
    if low.startswith(("http://", "https://", "hxxp://", "hxxps://")):
        return "url"
    if base is not None and any(i.id == value.strip() for i in base.incidents):
        return "dataset_item"
    return "keyword"


def _run_root() -> Path:
    root = os.environ.get("FTF_RUN_DIR")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
        return Path(root)
    return Path(tempfile.gettempdir())


def analyze(
    inp: AnalysisInput,
    base: Dataset,
    *,
    offline: bool = True,
    fetch: bool = False,
    run_dir: str | os.PathLike | None = None,
    keep_run_dir: bool = True,
    base_dir: str | os.PathLike | None = None,
) -> AnalysisResult:
    """Analyse one input against the campaign `base`. Never raises for stage failures.

    `offline=False` allows network enrichment (registries, DNS over HTTPS); it never contacts the
    page itself. `fetch=True` (and online) lets a URL be fetched live with `ftf.fetch`: only on the
    only when online and enabled (the container image sets FTF_FETCH=1), never on a VM.
    Input bytes are copied into a per-run directory (`run_dir`, else a fresh temp dir under
    $FTF_RUN_DIR or the system temp dir), hashed with sha256 and never executed or rendered.
    `base_dir` is the folder of the base's dataset.json (its vector stores), passed to linking.
    """
    from ftf.pipeline import stages  # late import: stages imports this module's types

    notes: list[str] = []
    if not offline:  # one short probe at the start; no network means the offline package
        from ftf.enrich.probe import network_up

        if not network_up():
            offline = True
            notes.append("network probe failed: enrichment uses the offline cache and the precomputed base")
    own_dir = run_dir is None
    rdir = Path(run_dir) if run_dir is not None else Path(tempfile.mkdtemp(prefix="ftf-run-", dir=_run_root()))
    rdir.mkdir(parents=True, exist_ok=True)
    ctx = stages.RunContext(inp=inp, base=base, offline=offline, run_dir=rdir, fetch=fetch,
                            base_dir=Path(base_dir) if base_dir is not None else None, notes=notes)
    reports: list[StageReport] = []
    for name, fn in stages.STAGES:
        start = time.perf_counter()
        try:
            status, note = fn(ctx)
        except Exception as exc:  # a broken stage must not take the app down
            status, note = "error", f"{exc.__class__.__name__}: {exc}"
        reports.append(StageReport(name, status, note, int((time.perf_counter() - start) * 1000)))
    stages.finish(ctx)
    if own_dir and not keep_run_dir:
        shutil.rmtree(rdir, ignore_errors=True)
    return AnalysisResult(
        input=inp,
        incident=ctx.incident,
        links=ctx.links,
        assignment=ctx.assignment,
        similar=stages.similar_items(ctx.incident, ctx.links, base),
        stages=reports,
        base=base,
        run_dir=rdir if keep_run_dir or not own_dir else None,
        input_root=ctx.input_root,
        from_base=ctx.from_base,
        notes=ctx.notes,
        enrichment=ctx.enrichment,
    )
