"""Stage adapters of `ftf analyze`: one small function per stage, in pipeline order.

Each adapter calls the owning project's library when it is importable and returns
`("not_available", note)` when it is not, so the app works end to end before every project has
landed. Owners: edit your adapter here when your public call changes; keep it one call site.

| stage | owner | call |
|---|---|---|
| ingest | Ingestion (HACK-9), fetch (HACK-17) | `ftf.ingest.ingest(inputs, root=..., name=..., indicators=True) -> Dataset`; URLs first `ftf.fetch.fetch_url(url, out_dir)` (online only) |
| content features | Features (HACK-13/14/15) | `ftf.features.pipeline.extract(incident, raw_root, out_dir) -> Incident` |
| enrichment | Enrichment (HACK-19) | `ftf.enrich.Enricher(Cache(path), offline=...).enrich(domain)` |
| link/assign | Linking (HACK-21/22) | `ftf.linking.link_incident(base, incident, base_dir=..., incident_dir=...)` |

Without a library the fallback keeps the result valid: the input becomes one capture of a new
incident (ad-hoc ids from `ftf.ingest.ids`, see `adhoc_ids`), the modalities it lacks are listed as
`missing` with a reason, and the decision is `review` with a note. A base item
(`dataset_item` with a base incident id) reuses the base's precomputed indicators, links and
assignment (the fixture backend).

Paths: artefact paths are relative to the input root (`RunContext.input_root`): the per-run
input dir for uploads, $FTF_RAW_ROOT (default /data/raw) for organiser case folders. The same
root goes to ingestion (`root`) and features (`raw_root`).
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ftf.contract import (
    Artefact,
    ArtefactType,
    Assignment,
    Capture,
    Dataset,
    Decision,
    Incident,
    Indicator,
    Link,
    MissingModality,
    Observed,
    Stage,
)
from ftf.enrich.normalise import registrable_domain
from ftf.indicators import _pages, extract_all
from ftf.indicators.domains import registered_domains
from ftf.ingest import ids
from ftf.pipeline.analyze import AnalysisInput, SimilarItem

METHOD = "ftf-pipeline@0.1"
MAX_FOLDER_FILES = 500
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_FILE_TYPES = {
    ".har": ArtefactType.har,
    ".html": ArtefactType.html,
    ".htm": ArtefactType.html,
    ".mhtml": ArtefactType.mhtml,
    ".mht": ArtefactType.mhtml,
    ".txt": ArtefactType.text,
    **{e: ArtefactType.screenshot for e in IMAGE_EXT},
    **{e: ArtefactType.video for e in VIDEO_EXT},
}
# What a complete report has. A submission lacking one lists it as missing (not a penalty).
FULL_REPORT = (ArtefactType.ad_screenshot, ArtefactType.html, ArtefactType.har, ArtefactType.video, ArtefactType.ad_library)
SCREENSHOT_TYPES = {ArtefactType.ad_screenshot, ArtefactType.screenshot}


@dataclass
class RunContext:
    inp: AnalysisInput
    base: Dataset
    offline: bool
    run_dir: Path
    fetch: bool = False  # live page fetching: sandbox only, never on the workbench or the jury's machine
    input_root: Optional[Path] = None  # artefact paths of the incident are relative to this
    base_dir: Optional[Path] = None  # folder of the base dataset.json: the base's vector stores
    incident: Optional[Incident] = None
    links: list[Link] = field(default_factory=list)
    assignment: Optional[Assignment] = None
    from_base: bool = False
    notes: list[str] = field(default_factory=list)
    enrichment: Optional[str] = None  # "online", "offline cache" or "mixed" (HACK-91)

    def __post_init__(self) -> None:
        if self.input_root is None:
            folder = self.inp.path if self.inp.kind == "dataset_item" else None
            if folder is None:
                self.input_root = self.run_dir / "input"
            else:
                folder = folder.resolve()
                raw = raw_root().resolve()
                self.input_root = raw if folder.is_relative_to(raw) else folder.parent


def _optional(module: str, name: str) -> Optional[Callable]:
    """`module.name` if the module is importable and has it, else None."""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # Only "the package is not installed" means not available; a broken dependency inside
        # an installed package is a real error and surfaces as the stage's `error` status.
        if exc.name is not None and (module == exc.name or module.startswith(exc.name + ".")):
            return None
        raise
    return getattr(mod, name, None)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "input"
    return stem[:120]


# -- ids (Ingestion, HACK-9, owns the scheme; see HACK-41) -----------------------------------


def adhoc_ids(digests_or_url: list[str] | str) -> tuple[str, str]:
    """(incident id, capture id) of an input ingestion did not handle: `input:<sha12>` and `…:c1`.

    `ftf.ingest.ids` owns the rule: sha12 over the sorted artefact sha256s, or over the URL.
    """
    inc_id = ids.input_incident_id(digests_or_url)
    return inc_id, ids.capture_id(inc_id, ids.SINGLE_CAPTURE_KEY)


# -- fallback incident ------------------------------------------------------------------------


def raw_root() -> Path:
    return Path(os.environ.get("FTF_RAW_ROOT") or "/data/raw")


def _store_input(ctx: RunContext) -> tuple[list[tuple[Path, str, ArtefactType]], list[str] | str]:
    """Copy (or, for a case folder, list) the input bytes. Returns (path, rel, type) and the id key.

    The id key is the URL for a URL input, else the artefact sha256s (`ftf.ingest.ids` rule).

    `rel` is relative to `ctx.input_root` (the per-run input dir, or the case folder's root).
    """
    inp = ctx.inp
    if inp.kind == "dataset_item" and inp.path is not None:  # a case folder: hashed in place, never copied or opened
        folder = inp.path.resolve()
        root = ctx.input_root.resolve()
        files = sorted(
            r for r in (p.resolve() for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
            if r.is_relative_to(root)  # nothing reached through a symlinked directory outside the root
        )[:MAX_FOLDER_FILES]
        out = [(p, p.relative_to(root).as_posix(), _FILE_TYPES.get(p.suffix.lower(), ArtefactType.asset)) for p in files]
        return out, [sha256_file(p) for p, _, _ in out]
    in_dir = ctx.run_dir / "input"
    in_dir.mkdir(parents=True, exist_ok=True)
    if inp.kind in ("url", "keyword") or inp.kind == "dataset_item":
        name = {"url": "url.txt", "keyword": "keyword.txt"}.get(inp.kind, "item.txt")
        target = in_dir / name
        target.write_text((inp.text or "").strip() + "\n", encoding="utf-8")
        typ = ArtefactType.url if inp.kind == "url" else ArtefactType.text
        key = (inp.text or "").strip() if inp.kind == "url" else [sha256_file(target)]
        return [(target, name, typ)], key
    target = _copy_upload(ctx)
    ext = target.suffix.lower()
    if inp.kind == "media":
        typ = ArtefactType.video if ext in VIDEO_EXT else ArtefactType.ad_screenshot if ext in IMAGE_EXT else ArtefactType.other
    else:
        typ = {
            "screenshot": ArtefactType.screenshot,
            "har": ArtefactType.har,
            "html": ArtefactType.html,
            "mhtml": ArtefactType.mhtml,
        }[inp.kind]
    return [(target, target.name, typ)], [sha256_file(target)]


def _copy_upload(ctx: RunContext) -> Path:
    in_dir = ctx.run_dir / "input"
    in_dir.mkdir(parents=True, exist_ok=True)
    target = in_dir / _safe_name(ctx.inp.name or ctx.inp.path.name)
    if not target.exists():
        shutil.copyfile(ctx.inp.path, target)
    return target


def _missing_reason(ctx: RunContext, modality: ArtefactType) -> str:
    if modality in (ArtefactType.html, ArtefactType.har) and ctx.inp.kind == "url":
        if ctx.offline or not ctx.fetch:
            return "not fetched: live pages are fetched only from the sandbox (HACK-17)"
        return "not fetched: the fetcher (HACK-17) is not available here"
    if modality is ArtefactType.ad_library:
        return "no Ad Library entry was submitted"
    return "not submitted"


def _fallback_incident(ctx: RunContext) -> Incident:
    stored, key = _store_input(ctx)
    inc_id, cap_id = adhoc_ids(key)
    artefacts = []
    seen: set[str] = set()
    for path, rel, typ in stored:
        art_id = ids.artefact_id(cap_id, rel)
        n = 1
        while art_id in seen:  # sanitising can map two file names to one id (`a b.png`, `a-b.png`)
            n += 1
            art_id = f"{ids.artefact_id(cap_id, rel)}-{n}"
        seen.add(art_id)
        artefacts.append(Artefact(id=art_id, type=typ, path=rel, sha256=sha256_file(path), size=path.stat().st_size))
    present = {a.type for a in artefacts}
    missing = []
    for modality in FULL_REPORT:
        if modality in present or (modality in SCREENSHOT_TYPES and present & SCREENSHOT_TYPES):
            continue
        missing.append(MissingModality(modality=modality, reason=_missing_reason(ctx, modality)))
    stage = Stage.ad if ctx.inp.kind == "media" else Stage.unknown
    capture = Capture(id=cap_id, stage=stage, observed=Observed.static, artefacts=artefacts, missing=missing)
    ref = ctx.inp.label if ctx.inp.kind in ("url", "keyword", "dataset_item") else ctx.inp.name or ctx.inp.label
    return Incident(id=inc_id, source="submitted", source_ref=ref[:300], captures=[capture])


def _add_indicators(incident: Incident, indicators: list[Indicator]) -> Incident:
    data = incident.model_dump()
    data["indicators"] = data["indicators"] + [i.model_dump() for i in indicators]
    return Incident.model_validate(data)


# -- stages -----------------------------------------------------------------------------------


def ingest(ctx: RunContext) -> tuple[str, str]:
    inp = ctx.inp
    if inp.kind == "dataset_item" and inp.path is None:
        wanted = (inp.text or "").strip()
        for inc in ctx.base.incidents:
            if inc.id == wanted:
                ctx.incident = inc
                ctx.from_base = True
                return "ok", f"incident {inc.id} taken from the base with its precomputed indicators"
        ctx.incident = _fallback_incident(ctx)
        return "error", f"incident id {wanted!r} is not in the base"

    if inp.kind == "keyword":
        ctx.incident = _fallback_incident(ctx)
        return "skipped", "a keyword has no file to ingest; stored as a text artefact"

    root = ctx.input_root
    if inp.kind == "url":
        fetch_url = _optional("ftf.fetch", "fetch_url") if ctx.fetch and not ctx.offline else None
        if fetch_url is None:
            ctx.incident = _fallback_incident(ctx)
            if not ctx.fetch or ctx.offline:
                why = "page fetching is off; live pages are fetched only from the sandbox"
            else:
                why = "the fetcher (HACK-17) is not available"
            return "skipped", f"URL not fetched ({why}); stored as a URL artefact"
        fetch_dir = ctx.run_dir / "fetch"
        fetch_url(refang(inp.text), fetch_dir)  # HACK-17: writes a capture directory; ingestion builds the Incident
        sources, root = [fetch_dir], fetch_dir
    elif inp.kind == "dataset_item":
        sources = [inp.path]
    else:
        sources = [_copy_upload(ctx)]

    ingest_fn = _optional("ftf.ingest", "ingest")
    if ingest_fn is None:
        ctx.incident = _fallback_incident(ctx)
        return "not_available", "ingestion (HACK-9) not available: the input is stored as one capture (hashed, not parsed)"
    # Ingestion (HACK-9): ingest(inputs, *, root=None, name, indicators=True) -> Dataset
    incidents = list(ingest_fn(sources, root=root, name="analyze", indicators=True).incidents)
    if not incidents:
        ctx.incident = _fallback_incident(ctx)
        return "error", "ingestion returned no incident; the input is stored as one capture"
    ctx.input_root = root
    extra = f"; {len(incidents) - 1} further incident(s) ignored" if len(incidents) > 1 else ""
    known = _in_base(ctx, incidents[0].id)
    if known is not None:  # the base already holds this case: linking it again would compare it with itself
        ctx.incident = known
        ctx.from_base = True
        same = _shas(known) == _shas(incidents[0])
        warn = "" if same else "; WARNING: the submitted files differ from the base's copy, which was not re-analysed"
        return "ok", f"ingested as {known.id}, which the base already holds: its precomputed indicators, links and assignment are used{warn}{extra}"
    ctx.incident = incidents[0]
    return "ok", f"ingested by ftf.ingest: {len(ctx.incident.captures)} capture(s), {len(ctx.incident.indicators)} indicator(s){extra}"


def _in_base(ctx: RunContext, incident_id: str) -> Optional[Incident]:
    return next((inc for inc in ctx.base.incidents if inc.id == incident_id), None)


def _shas(incident: Incident) -> list[str]:
    return sorted(a.sha256 or "" for c in incident.captures for a in c.artefacts)


def content_features(ctx: RunContext) -> tuple[str, str]:
    if ctx.from_base:
        return "skipped", "precomputed indicators from the base"
    # Content features (HACK-13/14/15): extract(incident, raw_root, out_dir) -> Incident.
    try:
        from ftf.features.pipeline import extract
    except ImportError:
        return "not_available", "content features (HACK-13/14/15) not available yet"
    before = len(ctx.incident.indicators)
    ctx.incident = extract(ctx.incident, ctx.input_root, ctx.run_dir / "features")
    return "ok", f"{len(ctx.incident.indicators) - before} indicator(s) added"


def refang(text: str) -> str:
    """Undo display defanging (`hxxp`, `[.]`) so a pasted, defanged URL can be looked up."""
    text = text.strip()
    if text[:4].lower() == "hxxp":
        text = "http" + text[4:]
    return text.replace("[.]", ".").replace("[:]", ":")


def _with_indicators(ctx: RunContext) -> Incident:
    """The incident with its indicators: ingestion's, or (fallback, ingestion did not run) extracted here."""
    if ctx.incident.indicators:
        return ctx.incident
    return extract_all(Dataset(name="analyze", incidents=[ctx.incident]), ctx.input_root).incidents[0]


def _domains(ctx: RunContext) -> list[tuple[str, str]]:
    """(capture id, URL) pairs to enrich: a URL input, else the scam's own pages.

    Own pages are `ftf.indicators._pages.page_chain` URLs whose registered domain is one of
    `registered_domains(dataset)`: landing, redirect hops and ad landings, without the shared
    CDNs, trackers and platform shims (`include_shared=False`).
    """
    if not ctx.incident.captures:
        return []
    if ctx.inp.kind == "url":
        return [(ctx.incident.captures[0].id, refang(ctx.inp.text))]
    if ctx.input_root is None:
        return []
    own = set(registered_domains(Dataset(name="analyze", incidents=[_with_indicators(ctx)]), include_shared=False))
    if not own:
        return []
    out: list[tuple[str, str]] = []
    for cap in ctx.incident.captures:
        for page in _pages.page_chain(cap, ctx.input_root):
            if registrable_domain(page.url) in own:
                out.append((cap.id, page.url))
    return list(dict.fromkeys(out))


def enrichment_mode(origins: Counter, offline: bool) -> str:
    """The HACK-91 label. "online": no answer needed the fallback. "offline cache": an offline run, or
    every answer fell back to the cache (stale entry or none). "mixed": some of each."""
    if offline:
        return "offline cache"
    fallen = origins["stale-cache"] + origins["none"]
    if fallen == 0:
        return "online"
    return "offline cache" if fallen == sum(origins.values()) else "mixed"


def enrichment(ctx: RunContext) -> tuple[str, str]:
    if ctx.from_base:
        return "skipped", "precomputed indicators from the base"
    domains = _domains(ctx)
    if not domains:
        return "skipped", "no domain known for this input (needs a URL or a HAR with an http(s) page URL)"
    try:
        from ftf.enrich import Cache, Enricher
        from ftf.enrich.commands import DEFAULT_CACHE
        from ftf.enrich.probe import ShortTimeoutClient
    except ImportError:
        return "not_available", "domain enrichment (HACK-19) not installed"
    cache_path = Path(DEFAULT_CACHE)
    cache = Cache(cache_path if cache_path.is_file() else None)  # never create a cache file as a side effect
    enricher = Enricher(cache, offline=ctx.offline, client=ShortTimeoutClient())
    added: list[Indicator] = []
    origins: Counter[str] = Counter()
    unknown = 0
    done: set[tuple[str, str]] = set()
    failed = 0
    for cap_id, dom in domains:
        name = enricher.registrable(dom)
        if name is None or (cap_id, name) in done:
            continue
        done.add((cap_id, name))
        try:
            res = enricher.enrich(name)
        except Exception:  # one bad domain must not drop what the others gave
            failed += 1
            continue
        origins.update(look.origin for look in res.lookups.values())
        for ind in res.incident.indicators:
            unknown += ind.value == "unknown"
            added.append(ind.model_copy(update={
                "id": f"{cap_id}:enrich:{len(added) + 1}",
                "capture_id": cap_id,
                "provenance": ind.provenance.model_copy(update={"artefact_id": None}),
            }))
    if not done:
        return "skipped", "no registrable domain in this input"
    ctx.incident = _add_indicators(ctx.incident, added)
    ctx.enrichment = enrichment_mode(origins, ctx.offline)
    where = ", ".join(f"{k}: {v}" for k, v in sorted(origins.items()))
    fail = f"; {failed} domain(s) failed" if failed else ""
    return "ok", f"enrichment: {ctx.enrichment}; {len(added)} indicator(s), {unknown} unknown; lookups {where}{fail}"


def link_assign(ctx: RunContext) -> tuple[str, str]:
    known = None if ctx.from_base else _in_base(ctx, ctx.incident.id)
    if known is not None:  # a fallback id the base already holds: never link an incident against itself
        ctx.incident = known
        ctx.from_base = True
    inc = ctx.incident
    if ctx.from_base:
        own = {c.id for c in inc.captures}
        ctx.links = [l for l in ctx.base.links if l.a in own or l.b in own]
        ctx.assignment = next((a for a in ctx.base.assignments if a.incident_id == inc.id), None)
        if ctx.assignment is None:
            ctx.assignment = _review(inc, "the base holds no assignment for this incident")
        return "ok", f"precomputed in the base: {len(ctx.links)} link(s), decision {ctx.assignment.decision.value}"
    # Linking (HACK-21/22); Evaluation's baseline uses the same call shape.
    # link_incident(base, incident, relation=campaign, backend=None, base_dir, incident_dir) -> IncidentResult
    link_incident = _optional("ftf.linking", "link_incident")
    if link_incident is None:
        ctx.assignment = _review(inc, "linking engine (HACK-21/22) not available: no link was scored, so this item needs review")
        return "not_available", "linking engine (HACK-21/22) not available yet; decision set to review"
    result = link_incident(ctx.base, inc, base_dir=ctx.base_dir, incident_dir=ctx.run_dir / "features")
    ctx.links = list(result.links)
    ctx.assignment = result.assignment
    extra = "" if ctx.base.campaigns else "; the base has no campaigns (build it with `ftf link`)"
    return "ok", f"{len(ctx.links)} link(s), decision {ctx.assignment.decision.value}{extra}"


def _review(incident: Incident, note: str) -> Assignment:
    return Assignment(incident_id=incident.id, candidates=[], decision=Decision.review, evidence_capacity_bits=0.0, method=METHOD, note=note)


STAGES: list[tuple[str, Callable[[RunContext], tuple[str, str]]]] = [
    ("ingest", ingest),
    ("content features", content_features),
    ("enrichment", enrichment),
    ("link/assign", link_assign),
]


def finish(ctx: RunContext) -> None:
    """Make the result complete even when a stage failed."""
    if ctx.incident is None:
        try:
            ctx.incident = _fallback_incident(ctx)
        except Exception as exc:  # e.g. an unreadable input file: still return a valid result
            # Own prefix: must not collide with a URL input or another unreadable upload.
            inc_id = ids.incident_id("input-error", ids.digest12(f"{ctx.inp.kind}:{ctx.inp.label}:{ctx.run_dir}"))
            cap_id = ids.capture_id(inc_id, ids.SINGLE_CAPTURE_KEY)
            ctx.incident = Incident(id=inc_id, source="submitted", source_ref=ctx.inp.label[:300],
                                    captures=[Capture(id=cap_id, stage=Stage.unknown)])
            ctx.notes.append(f"input could not be stored: {exc.__class__.__name__}: {exc}")
    if ctx.assignment is None or ctx.assignment.incident_id != ctx.incident.id:
        ctx.assignment = _review(ctx.incident, "a stage failed before a decision was made; see the stage list")
        ctx.links = []


def similar_items(incident: Incident, links: list[Link], base: Dataset) -> list[SimilarItem]:
    """Base captures linked to the incident, best link per capture, highest bits first."""
    own = {c.id for c in incident.captures}
    owner = {c.id: i.id for i in base.incidents for c in i.captures}
    best: dict[str, SimilarItem] = {}
    for l in links:
        if (l.a in own) == (l.b in own):
            continue
        mine, other = (l.a, l.b) if l.a in own else (l.b, l.a)
        item = SimilarItem(
            incident_id=owner.get(other, "?"),
            capture_id=other,
            own_capture_id=mine,
            score_bits=l.score_bits,
            relation=l.relation.value,
            level=l.level.value,
            link_id=l.id,
        )
        prev = best.get(other)
        if prev is None or _bits(item) > _bits(prev):
            best[other] = item
    return sorted(best.values(), key=lambda s: (-_bits(s), s.capture_id))


def _bits(item: SimilarItem) -> float:
    return item.score_bits if item.score_bits is not None else float("-inf")
