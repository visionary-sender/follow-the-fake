"""Indicator extractors over contract captures.

Every public module in this package that defines

    extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]

is run by `extract_all`, in module-name order, on each capture that is not an exact duplicate.
`root` is the directory artefact paths are relative to. Extractors may use any `id`;
`extract_all` reassigns ids, drops exact repeats, drops stop-listed values (if
`ftf.indicators._stoplist` exists) and redacts access tokens as a last line of defence.
Modules whose name starts with "_" are helpers, not extractors.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

from ftf.contract import Capture, Dataset, Incident, Indicator

# access_token as a URL parameter (plain or URL-encoded "=") or as a JSON key, and bare Meta
# "EAA..." tokens, also right after an encoded "=", "&" or quote (%3D, %26, %22).
_TOKEN_PARAM = re.compile(r"(access_token(?:=|%3D)|\"access_token\"\s*:\s*\")[^&\s\"'<>#,;}%]*", re.I)
_TOKEN_EAA = re.compile(r"(?:(?<=%3D)|(?<=%3d)|(?<=%26)|(?<=%22)|(?<![A-Za-z0-9]))EAA[A-Za-z0-9]{20,}")


def extractors() -> list[ModuleType]:
    mods = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if callable(getattr(mod, "extract", None)):
            mods.append(mod)
    return mods


def _stopped():
    try:
        from ftf.indicators._stoplist import is_stopped
    except ImportError:
        return lambda indicator: False
    return is_stopped


def redact(text: str) -> str:
    text = _TOKEN_PARAM.sub(lambda m: m.group(1) + "REDACTED", text)
    return _TOKEN_EAA.sub("REDACTED", text)


def extract_capture(incident: Incident, capture: Capture, root: Path, mods: Iterable[ModuleType]) -> list[Indicator]:
    is_stopped = _stopped()
    out: list[Indicator] = []
    seen: set[tuple] = set()
    for mod in mods:
        for ind in mod.extract(incident, capture, root):
            if ind.value is not None:
                ind = ind.model_copy(update={"value": redact(ind.value)})
            key = (ind.capture_id, ind.layer, ind.kind, ind.value, ind.observed)
            if key in seen or is_stopped(ind):
                continue
            seen.add(key)
            out.append(ind)
    return out


def extract_all(dataset: Dataset, root: Path) -> Dataset:
    """Return a copy of `dataset` whose incidents carry freshly extracted indicators."""
    mods = extractors()
    incidents = []
    for inc in dataset.incidents:
        found: list[Indicator] = []
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            found.extend(extract_capture(inc, cap, root, mods))
        found = [ind.model_copy(update={"id": f"{inc.id}:i{n:04d}"}) for n, ind in enumerate(found)]
        incidents.append(inc.model_copy(update={"indicators": found}))
    return Dataset.model_validate(dataset.model_copy(update={"incidents": incidents}).model_dump())
