"""Mint ids, decide stages and missing modalities, collapse exact duplicates, build the Dataset."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Capture, Dataset, Incident, MissingModality, Observed, Stage
from ftf.ingest import files as F
from ftf.ingest import ids
from ftf.ingest.model import AD_TYPES, RawCapture, RawIncident

PAGE_EXPECTED = (ArtefactType.html, ArtefactType.har)
MANIFEST_SUFFIX = ":manifest"
AD_EXPECTED = (ArtefactType.ad_screenshot, ArtefactType.video, ArtefactType.ad_library)


@dataclass
class CaptureRecord:
    """Per-capture bookkeeping for the CLI and the timing file (never written into the Dataset)."""

    incident_id: str
    capture_id: str
    stage: str
    duplicate_of: str | None
    artefacts: int
    load_seconds: float
    extract_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return self.load_seconds + self.extract_seconds


def _unique(key: str, used: set[str]) -> str:
    out, n = key, 1
    while out in used:
        n += 1
        out = f"{key}-{n}"
    used.add(out)
    return out


def _relpath(path: Path, root: Path) -> str:
    full = Path(path).resolve()
    rel = posixpath.normpath(full.relative_to(root).as_posix()) if full.is_relative_to(root) else None
    if rel is None:
        raise ValueError(f"{path} is outside the root {root}; pass --root to a folder that holds every input")
    return rel


# -- stages ------------------------------------------------------------------------------------


def _base_stage(cap: RawCapture) -> Stage:
    if cap.is_page():
        return Stage.lander
    if cap.types() & AD_TYPES:
        return Stage.ad
    return Stage.unknown


def _assign_stages(captures: list[RawCapture]) -> None:
    for cap in captures:
        if cap.stage is None:
            cap.stage = _base_stage(cap)
    # Decoy: a later visit to the same host whose saved HTML has no tracker or platform code,
    # while an earlier visit to that host had it (the cloaker served its white page).
    by_host: dict[str, list[RawCapture]] = {}
    for cap in captures:
        if cap.is_page() and cap.host:
            by_host.setdefault(cap.host, []).append(cap)
    for group in by_host.values():
        timed = sorted((c for c in group if c.started), key=lambda c: c.started)
        for i, cap in enumerate(timed):
            if cap.stage is not Stage.lander:
                continue
            marks = [inf.markers for inf in cap.page_infos(ArtefactType.html)]
            if not marks or any(marks):
                continue
            if any(any(inf.markers for inf in prev.page_infos(ArtefactType.html)) for prev in timed[:i]):
                cap.stage = Stage.decoy
                cap.notes.append("decoy: same host as an earlier capture, served without its tracker/platform code")


def _observed(cap: RawCapture) -> Observed:
    if cap.observed is not None:
        return cap.observed
    hars = cap.page_infos(ArtefactType.har)
    for inf in hars:
        if inf.local_file:
            cap.notes.append("HAR records opening a saved local file (file://), not live traffic")
    return Observed.live if any(inf.scheme in ("http", "https") for inf in hars) else Observed.static


def _captured_on(cap: RawCapture) -> str | None:
    if cap.captured_on:
        return cap.captured_on
    for inf in cap.page_infos(ArtefactType.har, ArtefactType.mhtml):
        if inf.day:
            return inf.day
    for a in cap.artefacts:
        if a.type in (ArtefactType.ad_screenshot, ArtefactType.screenshot, ArtefactType.video):
            day = F.date_in_name(Path(a.rel).name)
            if day:
                return day
    return None


def _missing(cap: RawCapture, incident_has_ad: bool) -> list[MissingModality]:
    if cap.missing is not None:
        return cap.missing
    present = cap.types()
    expected: list[ArtefactType] = []
    if cap.stage is Stage.ad or (cap.is_page() and not incident_has_ad):
        expected.extend(AD_EXPECTED)
    if cap.is_page():
        expected.extend(PAGE_EXPECTED)
    out = []
    for m in expected:
        if m in present:
            continue
        reason = cap.missing_reason
        if m is ArtefactType.video and any("deleted" in n for n in cap.notes):
            reason = "the ad video was deleted before capture"
        out.append(MissingModality(modality=m, reason=reason))
    return out


# -- assembly ----------------------------------------------------------------------------------


def build(raw: list[RawIncident], root: Path, name: str, description: str | None = None) -> tuple[Dataset, list[CaptureRecord]]:
    root = Path(root).resolve()
    incidents: list[Incident] = []
    records: list[CaptureRecord] = []
    used_incidents: set[str] = set()
    salts: dict[str, str] = {}
    for inc in raw:
        inc.captures = [c for c in inc.captures if c.artefacts or c.missing]
        if not inc.captures:
            continue
        if inc.fixed_id:
            if inc.fixed_id in used_incidents:  # the same fetch or ad record given twice: keep the first
                continue
            inc_id = inc.fixed_id
        elif inc.ref is not None:
            inc_id = ids.incident_id(inc.source, inc.ref)
        else:
            inc_id = ids.input_incident_id([a.sha256 for c in inc.captures for a in c.artefacts if a.sha256])
        inc_id = _unique(inc_id, used_incidents)
        _assign_stages(inc.captures)
        has_ad = any(c.stage is Stage.ad for c in inc.captures)
        single = len(inc.captures) == 1
        used_keys: set[str] = set()
        caps: list[Capture] = []
        for cap in inc.captures:
            if cap.fixed_id:
                cap_id = cap.fixed_id
                used_keys.add(cap_id)
            else:
                key = ids.SINGLE_CAPTURE_KEY if single else (ids.sanitise(cap.key) or "capture")
                cap_id = ids.capture_id(inc_id, _unique(key, used_keys))
            used_arts: set[str] = set()
            arts = []
            for a in cap.artefacts:
                aid = a.fixed_id if a.fixed_id else ids.artefact_id(cap_id, a.rel)
                aid = _unique(aid, used_arts)
                path = _relpath(a.path, root) if a.path is not None else None
                arts.append(Artefact(id=aid, type=a.type, path=path, sha256=a.sha256, size=a.size))
            observed = _observed(cap)
            if cap.dedup_salt:
                salts[cap_id] = cap.dedup_salt
            caps.append(Capture(id=cap_id, stage=cap.stage, observed=observed, captured_on=_captured_on(cap),
                                artefacts=arts, missing=_missing(cap, has_ad)))
            records.append(CaptureRecord(incident_id=inc_id, capture_id=cap_id, stage=cap.stage.value, duplicate_of=None,
                                         artefacts=len(arts), load_seconds=cap.load_seconds(), notes=list(dict.fromkeys(cap.notes))))
        incidents.append(Incident(id=inc_id, source=inc.source, source_ref=inc.source_ref, captures=caps))
    incidents = _collapse_duplicates(incidents, records, salts)
    return Dataset(name=name, description=description, incidents=incidents), records


def _collapse_duplicates(incidents: list[Incident], records: list[CaptureRecord], salts: dict[str, str] | None = None) -> list[Incident]:
    """Captures whose artefact contents (sha256 multiset) are identical point at the smallest id.

    ``salts`` keeps apart captures of the same content made differently on purpose (a default and a
    victim-profile fetch of a dead page), so each keeps its own collector indicators.
    """
    salts = salts or {}
    groups: dict[tuple[str, ...], list[str]] = {}
    for inc in incidents:
        for cap in inc.captures:
            # a collector manifest (``…:manifest``) differs on every run; it is not content
            content = [a for a in cap.artefacts if not (a.type is ArtefactType.other and a.id.endswith(MANIFEST_SUFFIX))]
            digests = tuple(sorted(a.sha256 for a in content if a.sha256))
            if digests and len(digests) == len(content):
                digests += (salts.get(cap.id, ""),)
                groups.setdefault(digests, []).append(cap.id)
    dup_of: dict[str, str] = {}
    for members in groups.values():
        if len(members) > 1:
            canon = ids.canonical(members)
            dup_of.update({m: canon for m in members if m != canon})
    if not dup_of:
        return incidents
    for rec in records:
        rec.duplicate_of = dup_of.get(rec.capture_id)
    out = []
    for inc in incidents:
        caps = [c.model_copy(update={"duplicate_of": dup_of.get(c.id)}) for c in inc.captures]
        out.append(inc.model_copy(update={"captures": caps}))
    return out
