"""docker/entrypoint.sh with arguments: `ftf <args>` gets FTF_BASE from the mounted base (no real ftf)."""

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"


@pytest.fixture
def fake_ftf(tmp_path):
    """A stand-in `ftf` on PATH that prints the FTF_BASE it was started with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ftf = bin_dir / "ftf"
    ftf.write_text('#!/bin/sh\necho "BASE=${FTF_BASE:-}"\n', encoding="utf-8")
    ftf.chmod(0o755)
    return bin_dir


def _run(fake_bin: Path, base_dir: Path, *args: str) -> str:
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "FTF_BASE_DIR": str(base_dir)}
    result = subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_arguments_use_the_mounted_base(fake_ftf, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "dataset.json").write_text("{}", encoding="utf-8")
    assert _run(fake_ftf, base, "analyze", "item") == f"BASE={base}/dataset.json"


def test_arguments_without_a_base_leave_it_unset(fake_ftf, tmp_path):
    assert _run(fake_ftf, tmp_path / "missing", "--help") == "BASE="
