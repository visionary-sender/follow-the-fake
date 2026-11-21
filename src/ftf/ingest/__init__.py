"""Ingestion: turn any supported input into contract captures (Dataset → Incident → Capture → Artefact).

Library API::

    from ftf.ingest import ingest, load_inputs, run
    ds = load_inputs(["/data/raw/do_udostępnienia", "/data/raw/BACKUP"], root=Path("/data/raw"))
    ds = ingest(["page.har"])                     # load_inputs + ftf.indicators.extract_all
    result = run(inputs, indicators=True)         # Dataset plus per-capture timing and notes

The unit of analysis is the **capture** (one visit or one saved item), not the folder.
Adapters and ids: :mod:`ftf.ingest.adapters`, :mod:`ftf.ingest.ids`. Artefact paths are relative
to ``root`` (default: the common parent of the inputs; for organiser data the raw data root).

Splitting a case folder into captures
-------------------------------------
1. Every sub-folder (other than a saved page's ``*_files``) is one capture keyed by its name
   (``61/strona_1``, a page-capture bundle, an automated archive). Top-level files named after
   it (``<archive>.zip``, ``<archive>.zip.sha256``) join it.
2. The remaining top-level page files (HTML, HAR, MHTML) are grouped by **site**: the host of
   the HTML's ``saved from url`` comment, the HAR's page URL or the MHTML's
   ``Snapshot-Content-Location``. A saved HTML takes its ``<stem>_files/`` folder (files there
   are ``asset`` artefacts, ``.pobrane`` suffixes ignored for typing). A page that names no host
   joins the only site in the folder. Folder 2 so yields two captures:
   ``acrosscommunities.live`` (HAR + saved page) and ``healthlinkq.pro`` (case 3's HAR).
3. Remaining top-level files (screenshots, videos, yt-dlp ``.info.json``, notes) are ad material:
   images are ``ad_screenshot``, ``.info.json`` is ``ad_library``. They join the site capture when
   the folder has exactly one, else they form their own capture (61's ``reklama_1``).
4. Capture key = the sub-folder name, else the primary file's stem (HAR, then HTML, then MHTML,
   then the first media file). A folder that is one capture uses ``c1``.

Stage heuristics
----------------
- ``lander``: the capture holds a page (HTML, HAR or MHTML).
- ``ad``: no page, only ad material (ad screenshot, video, ad-library record).
- ``decoy``: a page capture whose host was visited earlier in the same incident (by HAR/MHTML
  time) and whose saved HTML has none of the tracker/platform markers (``window.aio``, ``fbq(``,
  ``facebook.com/tr``, ``BINOM``, ``gtag(``, ``clickid``, ``connect.facebook.net``) the earlier
  visit had: the cloaker's white page (61 ``strona_3``). The fetcher's own ``decoy`` verdict is kept.
- ``pre_lander``: never inferred from static folders (a saved page does not show which hop it
  was); kept when a source states it.
- ``unknown``: anything else, e.g. a bare URL that was not fetched.

``observed`` is ``live`` when the capture holds a HAR of ``http(s)`` traffic, else ``static``. A HAR
whose page is a ``file://`` URL (61 ``strona_1/.har``: the analyst reopened the saved page) is a
local-file capture: ``static``, with a note in the timing report. Its URL is never copied out
(it holds the analyst's local path).

``missing`` lists absent modalities only: page captures expect ``html`` and ``har``; ad captures,
and page captures of an incident without an ad capture, expect ``ad_screenshot``, ``video`` and
``ad_library``. Exact duplicates (identical sha256 multiset of artefacts, across incidents too)
point ``duplicate_of`` at the lexicographically smallest capture id (BACKUP p2 → p1).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ftf.contract import Dataset
from ftf.ingest import adapters
from ftf.ingest.build import CaptureRecord, build
from ftf.ingest.model import RawIncident

__all__ = ["IngestResult", "CaptureRecord", "ingest", "load_inputs", "run", "default_root"]


@dataclass
class IngestResult:
    dataset: Dataset
    captures: list[CaptureRecord]
    seconds: float

    def summary(self) -> dict:
        times = [c.seconds for c in self.captures]
        return {
            "incidents": len(self.dataset.incidents),
            "captures": len(self.captures),
            "duplicates": sum(1 for c in self.captures if c.duplicate_of),
            "artefacts": sum(c.artefacts for c in self.captures),
            "indicators": len(self.dataset.indicators()),
            "seconds_total": round(self.seconds, 3),
            "seconds_per_capture_mean": round(sum(times) / len(times), 4) if times else 0.0,
            "seconds_per_capture_max": round(max(times), 4) if times else 0.0,
        }

    def timing(self) -> dict:
        return {
            "summary": self.summary(),
            "captures": [
                {"incident_id": c.incident_id, "capture_id": c.capture_id, "stage": c.stage, "duplicate_of": c.duplicate_of,
                 "artefacts": c.artefacts, "load_seconds": round(c.load_seconds, 4), "extract_seconds": round(c.extract_seconds, 4),
                 "seconds": round(c.seconds, 4), "notes": c.notes}
                for c in self.captures
            ],
        }


def _inputs(inputs: Sequence[str | Path]) -> list[str | Path]:
    seen: set[str] = set()
    out: list[str | Path] = []
    for item in inputs:
        key = item if isinstance(item, str) and adapters.is_url(item) else str(Path(item).resolve())
        if key not in seen:
            seen.add(key)
            out.append(item)
    paths = [Path(i).resolve() for i in out if not (isinstance(i, str) and adapters.is_url(i))]
    # An input inside another input (a fetcher folder under a fetcher root) is already covered.
    return [i for i in out if (isinstance(i, str) and adapters.is_url(i)) or not any(Path(i).resolve() != p and Path(i).resolve().is_relative_to(p) for p in paths)]


def default_root(inputs: Sequence[str | Path], extra: Sequence[Path] = ()) -> Path:
    anchors = [adapters.anchor(Path(i)).resolve() for i in inputs if not (isinstance(i, str) and adapters.is_url(i))]
    anchors += [Path(p).resolve() for p in extra]
    if not anchors:
        return Path.cwd().resolve()
    return Path(os.path.commonpath([str(a) for a in anchors]))


def run(
    inputs: Sequence[str | Path],
    *,
    root: Path | None = None,
    name: str = "ingest",
    indicators: bool = True,
    fetch: bool = False,
    fetch_dir: Path | None = None,
    fetcher: Callable | None = None,
) -> IngestResult:
    """Load ``inputs``, optionally extract indicators, and keep per-capture timing and notes."""
    t0 = time.perf_counter()
    items = _inputs(inputs)
    if fetch and fetch_dir is not None:
        Path(fetch_dir).mkdir(parents=True, exist_ok=True)
    raw: list[RawIncident] = []
    for item in items:
        raw.extend(adapters.load(item, fetch=fetch, fetch_dir=fetch_dir, fetcher=fetcher))
    if root is None:
        root = default_root(items, [fetch_dir] if fetch and fetch_dir is not None and any(isinstance(i, str) and adapters.is_url(i) for i in items) else [])
    dataset, records = build(raw, Path(root), name)
    if indicators:
        dataset = _extract(dataset, Path(root).resolve(), records)
    return IngestResult(dataset=dataset, captures=records, seconds=time.perf_counter() - t0)


def _extract(dataset: Dataset, root: Path, records: list[CaptureRecord]) -> Dataset:
    """Same result as ``ftf.indicators.extract_all``, timed per capture."""
    from ftf.indicators import extract_capture, extractors

    mods = extractors()
    by_id = {r.capture_id: r for r in records}
    incidents = []
    for inc in dataset.incidents:
        found = []
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            t0 = time.perf_counter()
            found.extend(extract_capture(inc, cap, root, mods))
            by_id[cap.id].extract_seconds += time.perf_counter() - t0
        found = [ind.model_copy(update={"id": f"{inc.id}:i{n:04d}"}) for n, ind in enumerate(found)]
        incidents.append(inc.model_copy(update={"indicators": found}))
    return Dataset.model_validate(dataset.model_copy(update={"incidents": incidents}).model_dump())


def load_inputs(inputs: Sequence[str | Path], *, root: Path | None = None, name: str = "ingest") -> Dataset:
    """Captures and artefacts only, no indicators."""
    return run(inputs, root=root, name=name, indicators=False).dataset


def ingest(inputs: Sequence[str | Path], *, root: Path | None = None, name: str = "ingest", indicators: bool = True) -> Dataset:
    """``load_inputs`` then ``ftf.indicators.extract_all`` (when ``indicators``)."""
    return run(inputs, root=root, name=name, indicators=indicators).dataset
