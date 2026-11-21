"""One entry point for all visual and content features: Incident or Dataset in, indicators added.

A family is a module ``ftf.features.<name>`` exposing ``apply(ds, raw_root, out_dir) -> Dataset``.
Families that are not installed yet are skipped, so this works while they land one by one.
Order matters: faces/brands may read text indicators, video reuses the still-image families.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from ftf.contract.models import Dataset, Incident

FAMILIES = ("text", "image", "faces", "video")


def available_families() -> list[str]:
    found = []
    for name in FAMILIES:
        try:
            mod = importlib.import_module(f"ftf.features.{name}")
        except ModuleNotFoundError as e:
            if e.name == f"ftf.features.{name}":
                continue
            raise
        if hasattr(mod, "apply"):
            found.append(name)
    return found


def apply_all(
    ds: Dataset, raw_root: Path, out_dir: Path, families: Optional[Sequence[str]] = None
) -> Dataset:
    """Run the families in order; indicators are appended in place, vectors go to out_dir."""
    names = list(families) if families else available_families()
    unknown = [n for n in names if n not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown feature families: {unknown}")
    for name in [n for n in FAMILIES if n in names]:
        ds = importlib.import_module(f"ftf.features.{name}").apply(ds, Path(raw_root), Path(out_dir))
    return ds


def extract(
    incident: Incident, raw_root: Path, out_dir: Path, families: Optional[Sequence[str]] = None
) -> Incident:
    """Integration call for the demo: one Incident in, the same Incident with indicators added."""
    ds = apply_all(Dataset(name="extract", incidents=[incident]), raw_root, out_dir, families)
    return ds.incidents[0]
