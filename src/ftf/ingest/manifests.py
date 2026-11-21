"""Capture folders written by our own collectors: the URL fetcher (HACK-17) and the ad-library scanner (HACK-20).

Both write ``<out>/<folder>/capture.json`` (a manifest with ``tool``, ``manifest_version`` and a
contract ``Incident``) next to the captured files, and ``<out>/dataset.json`` for the run. The
layout is detected from the manifest, never from file names:

- ``tool = ftf-fetch@…``: ``url.txt``, ``page.har``, ``page.html``, ``screenshot.png``. Ids are
  kept (``fetch:<capture_id>``, ``…:c1``, ``…:<type>``).
- ``tool = ftf-scanner@…``: ``ad.json`` plus ``creative-<n>.<ext>``. Ids are kept
  (``adlib:<id>`` / ``adlib:<id>:ad``, ``certpl:<domain>`` / ``certpl:<domain>:listing``).
  ``ad.json`` becomes the ``ad_library`` artefact (the manifest lists it without a path);
  creatives become ``ad_screenshot`` (images) or ``video``.
- CERT Polska bare-domain listings (``certpl:<registrable domain>``) are not ads and were never
  fetched: exactly one capture ``certpl:<domain>:listing``, stage ``unknown``, one pathless ``url``
  artefact whose sha256 is over the listed URL or domain from ``ad.json`` (no files), ``html``,
  ``har`` and ``screenshot`` missing with reason ``domain listing only, not fetched``, and incident
  source ``certpl``. Linking can skip page-level rarity for this source; the domain is in
  ``source_ref``.

Every other capture also gets its folder's ``capture.json`` as an ``other`` artefact
(``<capture id>:manifest``), so extractors can read collector facts such as the fetch profile.

Run folders: every child folder holding a ``capture.json`` is read; ``queue.jsonl``, ``state/``,
``runs/`` and ``dataset.json`` are ignored. A pre-v1 scanner output (``runs/<id>/incidents-*.json``
without capture folders) is refused. Then:

- across all inputs of one ingest call, fetches of the same requested URL (the default visit and
  an opt-in victim-profile re-fetch of a cloaked page, often in another run folder) form one
  incident under the first fetch's id (default profile first, then start time), with captures
  ``…:c1``, ``…:c2`` (:func:`merge_fetches`);
- within one run, a stable scanner id seen in several scan folders keeps the newest folder.

Manifests are untrusted: an artefact path that is absolute, contains ``..`` or a backslash, or
resolves outside its capture folder (symlinks included) is ignored. Nothing is executed or rendered.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ftf.contract import ArtefactType, Incident, MissingModality, Stage
from ftf.ingest import files as F
from ftf.ingest.model import RawArtefact, RawCapture, RawIncident

log = logging.getLogger(__name__)

MANIFEST = "capture.json"
AD_RECORD = "ad.json"
FETCH_TOOL = "ftf-fetch@"
SCAN_TOOL = "ftf-scanner@"
MANIFEST_VERSIONS = frozenset({1})
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
DEFAULT_PROFILE = "default"
CERTPL_PREFIX = "certpl:"
CERTPL_SOURCE = "certpl"
CERTPL_MISSING_REASON = "domain listing only, not fetched"


@dataclass
class Manifest:
    folder: Path
    data: dict
    tool: str  # "fetch" or "scan"
    incident: Incident

    @property
    def requested_url(self) -> str | None:
        url = self.data.get("requested_url")
        return url if isinstance(url, str) and url else None

    @property
    def profile(self) -> str:
        return profile_name(self.data)

    @property
    def started(self) -> str:
        s = self.data.get("started_at")
        return s if isinstance(s, str) else ""


def profile_name(data: dict) -> str:
    """The fetch profile's name; ``default`` when the manifest predates profiles."""
    p = data.get("profile")
    if isinstance(p, dict):
        p = p.get("name")
    return p if isinstance(p, str) and p else DEFAULT_PROFILE


def has_manifest(path: Path) -> bool:
    m = path / MANIFEST
    return path.is_dir() and not path.is_symlink() and m.is_file() and not m.is_symlink()


def read_manifest(folder: Path) -> Manifest:
    """Parse and check ``<folder>/capture.json``; ``ValueError`` if it is not one of ours."""
    path = folder / MANIFEST
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"{path}: manifest larger than {MAX_MANIFEST_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{path}: unreadable manifest ({e.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest is not a JSON object")
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool.startswith((FETCH_TOOL, SCAN_TOOL)):
        raise ValueError(f"{path}: unknown capture tool {str(tool)[:40]!r} (expected {FETCH_TOOL}… or {SCAN_TOOL}…)")
    if data.get("manifest_version") not in MANIFEST_VERSIONS:
        raise ValueError(f"{path}: unsupported manifest_version {data.get('manifest_version')!r}")
    try:
        incident = Incident.model_validate(data.get("incident"))
    except ValueError as e:
        raise ValueError(f"{path}: invalid incident ({e.__class__.__name__})") from None
    return Manifest(folder=folder, data=data, tool="fetch" if tool.startswith(FETCH_TOOL) else "scan", incident=incident)


# -- untrusted artefact paths -------------------------------------------------------------------


def safe_parts(path: object) -> tuple[str, ...] | None:
    """Relative POSIX path parts, or None for anything absolute, escaping or odd."""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        return None
    p = PurePosixPath(path)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        return None
    parts = tuple(part for part in p.parts if part != ".")
    return parts or None


def _inside(folder: Path, parts: tuple[str, ...]) -> Path | None:
    cand = folder
    for part in parts:
        cand = cand / part
        if cand.is_symlink():
            return None
    try:
        if not cand.is_file() or not cand.resolve().is_relative_to(folder.resolve()):
            return None
    except OSError:
        return None
    return cand


def artefact_file(folder: Path, path: object) -> Path | None:
    """A manifest path (relative to the run folder, ``<folder>/<file>``, or to the folder) as a file inside ``folder``."""
    parts = safe_parts(path)
    if parts is None:
        return None
    # ``<folder>/<file>`` first, then the path as given, then (a renamed folder) without its first part
    tries = [parts[1:]] if len(parts) > 1 and parts[0] == folder.name else []
    tries.append(parts)
    if len(parts) > 1 and parts[0] != folder.name:
        tries.append(parts[1:])
    for t in tries:
        found = _inside(folder, t)
        if found is not None:
            return found
    return None


# -- one capture folder -------------------------------------------------------------------------


def _load(path: Path, folder: Path, atype: ArtefactType, fixed_id: str) -> RawArtefact:
    from ftf.ingest.adapters import load_file  # adapters imports this module

    art = load_file(path, folder, ad_side=False, in_assets=False)
    art.type, art.fixed_id = atype, fixed_id
    return art


def _scan_type(path: Path, declared: ArtefactType) -> ArtefactType:
    t = F.artefact_type(path, in_assets=False, ad_side=True)
    return t if t in (ArtefactType.ad_screenshot, ArtefactType.video, ArtefactType.ad_library) else declared


def is_certpl(m: Manifest) -> bool:
    return m.tool == "scan" and (m.data.get("source") == "certpl" or m.incident.id.startswith(CERTPL_PREFIX))


def certpl_raw(m: Manifest) -> RawIncident:
    """A CERT Polska bare-domain listing: one ``…:listing`` capture holding one pathless ``url`` artefact."""
    inc = m.incident
    cap = inc.captures[0]
    record = {}
    p = artefact_file(m.folder, AD_RECORD)
    if p is not None and p.stat().st_size <= MAX_MANIFEST_BYTES:
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            record = {}
    record = record if isinstance(record, dict) else {}
    domain = inc.id[len(CERTPL_PREFIX):]
    value = next((v for v in (record.get("landing_url"), record.get("landing_domain")) if isinstance(v, str) and v), domain)
    data = value.encode("utf-8")
    lib = next((a.id for a in cap.artefacts if a.path is None), f"{cap.id}:library")
    art = RawArtefact(rel="url", type=ArtefactType.url, sha256=hashlib.sha256(data).hexdigest(), size=len(data), fixed_id=lib)
    missing = [MissingModality(modality=t, reason=CERTPL_MISSING_REASON) for t in (ArtefactType.html, ArtefactType.har, ArtefactType.screenshot)]
    raw_cap = RawCapture(key="listing", artefacts=[art], stage=Stage.unknown, observed=cap.observed, captured_on=cap.captured_on,
                         missing=missing, fixed_id=cap.id)
    return RawIncident(source=CERTPL_SOURCE, source_ref=domain, captures=[raw_cap], fixed_id=inc.id)


def to_raw(m: Manifest) -> RawIncident:
    """The manifest's incident with files re-read from disk (sha256 and size recomputed)."""
    if is_certpl(m):
        return certpl_raw(m)
    folder, inc = m.folder, m.incident
    captures: list[RawCapture] = []
    used: set[Path] = set()
    for n, c in enumerate(inc.captures):
        arts: list[RawArtefact] = []
        notes: list[str] = []
        for a in c.artefacts:
            if m.tool == "scan" and a.path is None and a.type in (ArtefactType.ad_library, ArtefactType.url):
                p = artefact_file(folder, AD_RECORD)  # the listed ad-library record has no path
                atype = ArtefactType.ad_library
            elif a.path is None:
                continue
            else:
                p = artefact_file(folder, a.path)
                atype = _scan_type(p, a.type) if (m.tool == "scan" and p is not None) else a.type
            if p is None or p in used:
                if a.path is not None or m.tool == "scan":
                    notes.append(f"{a.id}: artefact file missing or outside the capture folder; skipped")
                continue
            used.add(p)
            arts.append(_load(p, folder, atype, a.id))
        if m.tool == "scan" and n == 0:
            arts.extend(_unlisted_scan_files(folder, c.id, used))
        if n == 0:
            arts.append(_load(folder / MANIFEST, folder, ArtefactType.other, f"{c.id}:manifest"))
        present = {a.type for a in arts}
        missing = [x for x in c.missing if x.modality not in present]
        stage = c.stage
        if m.tool == "scan" and stage is Stage.unknown and present & {ArtefactType.ad_screenshot, ArtefactType.video}:
            stage = Stage.ad
        captures.append(RawCapture(key=c.id.rsplit(":", 1)[-1], artefacts=arts, stage=stage, observed=c.observed,
                                   captured_on=c.captured_on, missing=missing, notes=notes, fixed_id=c.id))
    raw = RawIncident(source=inc.source, source_ref=inc.source_ref, captures=captures, fixed_id=inc.id)
    if m.tool == "fetch":
        raw.fetch_url = m.requested_url
        raw.fetch_rank = (m.profile != DEFAULT_PROFILE, m.started, folder.name)
        raw.origin = str(folder.resolve())
    return raw


def _unlisted_scan_files(folder: Path, capture_id: str, used: set[Path]) -> list[RawArtefact]:
    """``ad.json`` and ``creative-*`` files the manifest does not list (an older or newer scanner)."""
    out = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name):
        if p in used or p.is_symlink() or not p.is_file():
            continue
        if p.name == AD_RECORD:
            out.append(_load(p, folder, ArtefactType.ad_library, f"{capture_id}:library"))
        elif p.name.startswith("creative-"):
            t = _scan_type(p, ArtefactType.other)
            if t is not ArtefactType.other:
                out.append(_load(p, folder, t, f"{capture_id}:{p.name.rsplit('.', 1)[0]}"))
        else:
            continue
        used.add(p)
    return out


def load_folder(folder: Path) -> RawIncident:
    return to_raw(read_manifest(folder))


# -- a run folder -------------------------------------------------------------------------------


def capture_folders(run: Path) -> list[Path]:
    from ftf.ingest.adapters import _listdir

    return [p for p in _listdir(run) if has_manifest(p)]


def is_run(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and bool(capture_folders(path))


def merge_fetches(raws: list[RawIncident]) -> list[RawIncident]:
    """Fetches of one requested URL (or sharing an incident id) across one ingest call become one incident.

    The fetcher writes a victim-profile re-fetch as its own incident, often into another run folder;
    here it joins the first fetch's incident (default profile first, then start time) as ``…:c2``.
    Other incidents keep their place; a folder given twice is read once.
    """
    groups: list[list[RawIncident]] = []
    slots: list[int | RawIncident] = []
    by_key: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    for r in raws:
        if not r.fetch_rank:
            slots.append(r)
            continue
        if r.origin is not None:
            if r.origin in seen:
                continue
            seen.add(r.origin)
        keys = [("id", r.fixed_id or "")] + ([("url", r.fetch_url)] if r.fetch_url else [])
        hits = sorted({by_key[k] for k in keys if k in by_key})
        if not hits:
            groups.append([r])
            idx = len(groups) - 1
            slots.append(idx)
        else:
            idx = hits[0]
            groups[idx].append(r)
            for other in hits[1:]:  # r bridges two groups
                groups[idx].extend(groups[other])
                groups[other] = []
                for k, v in by_key.items():
                    if v == other:
                        by_key[k] = idx
        for k in keys:
            by_key[k] = idx
    out: list[RawIncident] = []
    for s in slots:
        if isinstance(s, int):
            if groups[s]:
                out.append(_merge(groups[s]))
        else:
            out.append(s)
    return out


def _merge(group: list[RawIncident]) -> RawIncident:
    raws = sorted(group, key=lambda r: r.fetch_rank)
    if len(raws) == 1:
        return raws[0]
    first = raws[0]
    caps = [c for r in raws for c in r.captures]
    cap_ids = [c.fixed_id for c in caps]
    if not (all(r.fixed_id == first.fixed_id for r in raws) and len(set(cap_ids)) == len(cap_ids)):
        for n, c in enumerate(caps, start=1):
            new_id = f"{first.fixed_id}:c{n}"
            if new_id == c.fixed_id:
                continue
            c.notes.append(f"fetch {c.fixed_id} merged into {first.fixed_id} (same requested URL)")
            c.key, c.fixed_id = f"c{n}", new_id
            for a in c.artefacts:  # artefact ids follow their capture, so copied folders cannot collide
                a.fixed_id = f"{new_id}:{a.fixed_id.rsplit(':', 1)[-1]}" if a.fixed_id else None
    return RawIncident(source=first.source, source_ref=first.source_ref, captures=caps, fixed_id=first.fixed_id,
                       fetch_url=first.fetch_url, fetch_rank=first.fetch_rank, origin=first.origin)


def _newest_scans(manifests: list[Manifest]) -> list[Manifest]:
    best: dict[str, Manifest] = {}
    for m in manifests:
        key = (m.data.get("finished_at") if isinstance(m.data.get("finished_at"), str) else "", m.folder.name)
        cur = best.get(m.incident.id)
        if cur is None or key > (cur.data.get("finished_at") if isinstance(cur.data.get("finished_at"), str) else "", cur.folder.name):
            best[m.incident.id] = m
    keep = {id(m) for m in best.values()}
    return [m for m in manifests if id(m) in keep]


def is_legacy_scanner_root(path: Path) -> bool:
    """A pre-v1 scanner output (``runs/<id>/incidents-<source>.json``, hash-named creatives, no capture folders)."""
    runs = path / "runs"
    return (path / "queue.jsonl").is_file() and runs.is_dir() and any(runs.glob("*/incidents-*.json"))


def load_run(run: Path) -> list[RawIncident]:
    """Every capture folder in a fetcher or scanner run folder (bad manifests are skipped with a warning)."""
    fetches: list[Manifest] = []
    scans: list[Manifest] = []
    for folder in capture_folders(run):
        try:
            m = read_manifest(folder)
        except ValueError as e:
            log.warning("ftf ingest: skipping %s", e)
            continue
        (fetches if m.tool == "fetch" else scans).append(m)
    return [to_raw(m) for m in fetches] + [to_raw(m) for m in _newest_scans(scans)]
