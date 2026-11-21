"""Load the private v0 fixture (not in git; pull it from the bucket, see docs/contract.md)."""

from __future__ import annotations

import os
from pathlib import Path

from ftf.contract.models import Dataset

DEFAULT_DIR = Path("data/fixture/v0")
ENV_VAR = "FTF_FIXTURE_DIR"


def fixture_dir(path: str | os.PathLike | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(ENV_VAR) or DEFAULT_DIR)


def load_fixture(path: str | os.PathLike | None = None) -> Dataset:
    """Read and validate the fixture document `dataset.json` from the fixture directory.

    Resolution order: `path`, then $FTF_FIXTURE_DIR, then ./data/fixture/v0.
    Raises FileNotFoundError if absent and pydantic.ValidationError if invalid.
    """
    file = fixture_dir(path) / "dataset.json"
    return Dataset.from_json(file.read_bytes())
