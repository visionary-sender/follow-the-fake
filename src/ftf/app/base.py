"""Campaign base loader shared by the app pages."""

from __future__ import annotations

import os
from pathlib import Path

from ftf.contract import Dataset
from ftf.contract.fixture import fixture_dir, load_fixture


def load_base(path: str | os.PathLike | None = None) -> Dataset:
    """Campaign base: `path`, else $FTF_BASE (a dataset.json file), else the fixture (load_fixture())."""
    chosen = path if path is not None else os.environ.get("FTF_BASE")
    if chosen:
        with open(chosen, "rb") as f:
            return Dataset.from_json(f.read())
    return load_fixture()


def base_dir(path: str | os.PathLike | None = None) -> Path:
    """Folder of the campaign base chosen as in `load_base` (where its vector stores live)."""
    chosen = path if path is not None else os.environ.get("FTF_BASE")
    return Path(chosen).resolve().parent if chosen else fixture_dir()
