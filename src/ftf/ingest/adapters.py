"""Input adapters: turn a path or URL into raw incidents (no ids yet).

Supported inputs, each also accepted on its own:

1. an organiser dataset root (``do_udostępnienia`` or ``BACKUP``): one incident per case folder;
2. a single organiser case folder, or any other folder (an ad-hoc submission);
3. a saved HTML page, with its ``<stem>_files/`` resource folder if present;
4. a HAR file; 5. an MHTML file (kept as one ``mhtml`` artefact; only its MIME headers are read);
6. a bare ``http(s)://`` URL (fetched only with ``fetch=True`` and the HACK-17 fetcher installed);
7. a fetcher or scanner capture folder (``capture.json`` inside) or a run folder holding several
   (:mod:`ftf.ingest.manifests`).

Any other single file (a screenshot, a video, a yt-dlp ``.info.json``) becomes a one-artefact capture.
File names are untrusted: they are only ever used as path components and sanitised ids.
"""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

from ftf.contract import ArtefactType, MissingModality, Stage
from ftf.indicators import redact
from ftf.ingest import files as F
from ftf.ingest import ids, manifests
from ftf.ingest.model import RawArtefact, RawCapture, RawIncident

ORGANISER_SOURCES = {"do_udostepnienia", "BACKUP"}
URL_MISSING_REASON = "not fetched"
# A `<stem>_files` folder whose stem matches no saved page joins the page sharing at least this
# many leading characters (browsers truncate long stems), else it stays its own unit.
MIN_STEM_PREFIX = 8


def organiser_source(path: Path) -> str | None:
    """``do_udostepnienia`` / ``BACKUP`` if ``path`` is an organiser dataset root."""
    name = ids.sanitise(unicodedata.normalize("NFC", path.name))
    return name if name in ORGANISER_SOURCES else None


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://")) and "\n" not in text


def _listdir(folder: Path) -> list[Path]:
    """Children sorted by NFC name; symlinks and OS junk files are skipped (never followed)."""
    out = [p for p in folder.iterdir() if not p.is_symlink() and not F.is_junk(p.name)]
    return sorted(out, key=lambda p: unicodedata.normalize("NFC", p.name))


def _walk(folder: Path) -> list[Path]:
    out: list[Path] = []
    for p in _listdir(folder):
        if p.is_dir():
            out.extend(_walk(p))
        elif p.is_file():
            out.append(p)
    return out


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _in_assets(rel: str) -> bool:
    return any(part.endswith("_files") or part == "resources" for part in Path(rel).parts[:-1])


def load_file(path: Path, base: Path, *, ad_side: bool, in_assets: bool | None = None) -> RawArtefact:
    t0 = time.perf_counter()
    rel = _rel(path, base)
    if in_assets is None:
        in_assets = _in_assets(rel)
    atype = F.artefact_type(path, in_assets=in_assets, ad_side=ad_side)
    digest, size = F.sha256_file(path)
    info = F.page_info(path, atype)
    return RawArtefact(rel=rel, type=atype, path=path, sha256=digest, size=size, info=info, seconds=time.perf_counter() - t0)


def _stem(path: Path) -> str:
    name = F.clean_name(path.name)
    e = F.ext(name)
    if name.lower().endswith(".info.json"):
        return name[: -len(".info.json")]
    return name[: -len(e)] if e and name.lower().endswith(e) and len(name) > len(e) else name


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# -- one case folder ---------------------------------------------------------------------------


def split_folder(folder: Path) -> list[RawCapture]:
    """Split one case folder into captures (see the rules in :mod:`ftf.ingest`)."""
    children = _listdir(folder)
    subdirs = [p for p in children if p.is_dir() and not p.name.endswith("_files")]
    files_dirs = [p for p in children if p.is_dir() and p.name.endswith("_files")]
    top = [p for p in children if p.is_file()]

    captures: list[RawCapture] = []
    for sub in subdirs:
        cap = RawCapture(key=sub.name)
        inner = _walk(sub)
        has_page = any(F.ext(p) in F.PAGE_EXT and not _in_assets(_rel(p, folder)) for p in inner)
        cap.artefacts = [load_file(p, folder, ad_side=not has_page) for p in inner]
        captures.append(cap)
    # Files named after a sub-folder (an archive's .zip and .zip.sha256) belong to it.
    rest: list[Path] = []
    for p in top:
        owner = next((c for c in captures if _nfc(p.name).startswith(_nfc(c.key) + ".")), None)
        if owner is not None:
            owner.artefacts.append(load_file(p, folder, ad_side=False))
        else:
            rest.append(p)

    pages = [p for p in rest if F.ext(p) in F.PAGE_EXT]
    media = [p for p in rest if p not in pages]
    htmls = [p for p in pages if F.ext(p) in F.HTML_EXT]

    # Each page file is a unit; a saved HTML carries its resource folder.
    units: list[tuple[Path, list[Path]]] = [(p, []) for p in pages]
    for d in files_dirs:
        base = _nfc(d.name[: -len("_files")])
        target = next((h for h in htmls if _nfc(_stem(h)) == base), None)
        if target is None and len(htmls) == 1:
            target = htmls[0]
        if target is None and htmls:
            best = max(htmls, key=lambda h: len(os.path.commonprefix([_nfc(_stem(h)), base])))
            if len(os.path.commonprefix([_nfc(_stem(best)), base])) >= MIN_STEM_PREFIX:
                target = best
        if target is None:
            units.append((d, []))  # resources without a page: keep them as their own unit
        else:
            next(u for u in units if u[0] == target)[1].append(d)

    groups: list[RawCapture] = []
    by_host: dict[str, RawCapture] = {}
    unhosted: list[RawCapture] = []
    for anchor, extra in units:
        arts = [load_file(anchor, folder, ad_side=False)] if anchor.is_file() else []
        for d in ([anchor] if anchor.is_dir() else []) + extra:
            arts.extend(load_file(p, folder, ad_side=False, in_assets=True) for p in _walk(d))
        host = next((a.info.host for a in arts if a.info is not None and a.info.host), None)
        if host is not None and host in by_host:
            by_host[host].artefacts.extend(arts)
            continue
        cap = RawCapture(key="", artefacts=arts)
        groups.append(cap)
        if host is not None:
            by_host[host] = cap
        else:
            unhosted.append(cap)
    if len(by_host) == 1 and unhosted:  # a page that names no host joins the only site in the folder
        (only,) = by_host.values()
        for cap in unhosted:
            only.artefacts.extend(cap.artefacts)
            groups.remove(cap)
    for cap in groups:
        cap.key = _primary_key(cap)

    media_arts = [load_file(p, folder, ad_side=True) for p in media]
    if media_arts:
        if len(groups) == 1:
            groups[0].artefacts.extend(media_arts)
        else:
            groups.append(RawCapture(key=_stem(Path(media_arts[0].rel)), artefacts=media_arts))
    captures.extend(groups)
    for cap in captures:
        cap.artefacts.sort(key=lambda a: _nfc(a.rel))
        _deleted_markers(cap)
    return captures


def _primary_key(cap: RawCapture) -> str:
    for t in (ArtefactType.har, ArtefactType.html, ArtefactType.mhtml):
        for a in cap.artefacts:
            if a.type is t:
                stem = _stem(Path(a.rel))
                if ids.sanitise(stem):
                    return stem
    return _stem(Path(cap.artefacts[0].rel)) if cap.artefacts else "page"


def _deleted_markers(cap: RawCapture) -> None:
    for a in cap.artefacts:
        name = Path(a.rel).name.lower()
        if a.type is ArtefactType.text and a.size == 0 and "deleted" in name:
            cap.notes.append(f"{Path(a.rel).name}: the submitter marks a deleted item")


def case_folder(folder: Path, source: str | None) -> RawIncident:
    captures = split_folder(folder)
    if source is not None:
        return RawIncident(source=source, source_ref=_nfc(folder.name), ref=_nfc(folder.name), captures=captures)
    return RawIncident(source="input", source_ref=_nfc(folder.name), captures=captures)


def organiser_root(root: Path, source: str) -> list[RawIncident]:
    out = []
    for p in _listdir(root):
        if p.is_dir():
            out.append(case_folder(p, source))
    return out


# -- single files and URLs ---------------------------------------------------------------------


def single_file(path: Path) -> RawIncident:
    base = path.parent
    e = F.ext(path)
    arts = [load_file(path, base, ad_side=e not in F.PAGE_EXT, in_assets=False)]
    if e in F.HTML_EXT:
        res = base / f"{_stem(path)}_files"
        if res.is_dir() and not res.is_symlink():
            arts.extend(load_file(p, base, ad_side=False, in_assets=True) for p in _walk(res))
    cap = RawCapture(key=_stem(path), artefacts=arts)
    _deleted_markers(cap)
    return RawIncident(source="input", source_ref=_nfc(path.name), captures=[cap])


def bare_url(url: str) -> RawIncident:
    data = url.encode("utf-8")
    art = RawArtefact(rel="url", type=ArtefactType.url, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    cap = RawCapture(key=ids.SINGLE_CAPTURE_KEY, artefacts=[art], stage=Stage.unknown, missing_reason=URL_MISSING_REASON)
    cap.missing = [MissingModality(modality=m, reason=URL_MISSING_REASON) for m in (ArtefactType.html, ArtefactType.har, ArtefactType.screenshot)]
    return RawIncident(source="input", source_ref=redact(url), captures=[cap], fixed_id=ids.input_incident_id(url))


def fetch_available() -> bool:
    try:
        import ftf.fetch  # noqa: F401
    except ImportError:
        return False
    return True


def fetched_url(url: str, fetch_dir: Path, fetcher: Callable | None = None) -> RawIncident:
    """Capture ``url`` with the HACK-17 fetcher (``ftf.fetch.fetch_url``), then read its folder."""
    if fetcher is None:
        from ftf.fetch import fetch_url as fetcher
    result = fetcher(url, fetch_dir)
    return manifests.load_folder(Path(result.capture_dir))


# -- dispatch ----------------------------------------------------------------------------------


def anchor(path: Path) -> Path:
    """The directory artefact paths are naturally relative to (used for the default root)."""
    if path.is_dir():
        if organiser_source(path):
            return path.parent
        if organiser_source(path.parent):
            return path.parent.parent
        return path.parent
    return path.parent


def load(item: str | Path, *, fetch: bool = False, fetch_dir: Path | None = None, fetcher: Callable | None = None) -> list[RawIncident]:
    if isinstance(item, str) and is_url(item):
        if fetch:
            if fetcher is None and not fetch_available():
                raise RuntimeError("--fetch needs the HACK-17 fetcher (ftf.fetch), which is not installed")
            if fetch_dir is None:
                raise ValueError("fetching a URL needs a fetch directory")
            return [fetched_url(item, fetch_dir, fetcher)]
        return [bare_url(item)]
    path = Path(item)
    if path.is_symlink():
        raise ValueError(f"refusing to follow a symlink input: {path}")
    if path.is_dir():
        src = organiser_source(path)
        if src:
            return organiser_root(path, src)
        if manifests.has_manifest(path):
            return [manifests.load_folder(path)]
        if manifests.is_run(path):
            return manifests.load_run(path)
        subs = [p for p in _listdir(path) if p.is_dir()]
        if subs and all(organiser_source(p) for p in subs):  # the raw data root itself
            return [inc for p in subs for inc in organiser_root(p, organiser_source(p))]
        return [case_folder(path, organiser_source(path.parent))]
    if path.is_file():
        return [single_file(path)]
    raise FileNotFoundError(f"input not found: {item}")
