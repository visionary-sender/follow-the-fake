"""docker/entrypoint.sh with arguments: `ftf <args>` gets FTF_BASE from the mounted base (no real ftf)."""

import os
import shutil
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


PACKAGE_VARS = ("FTF_BASE", "FTF_MODEL_DIR", "FTF_FACE_REFERENCE", "FTF_BRAND_STOPLIST", "FTF_ENRICH_CACHE")


@pytest.fixture
def env_ftf(tmp_path):
    """A stand-in `ftf` on PATH that prints the package settings it was started with, one per line."""
    bin_dir = tmp_path / "envbin"
    bin_dir.mkdir()
    ftf = bin_dir / "ftf"
    lines = "\n".join(f'echo "{name}=${{{name}:-}}"' for name in PACKAGE_VARS)
    ftf.write_text(f"#!/bin/sh\n{lines}\n", encoding="utf-8")
    ftf.chmod(0o755)
    return bin_dir


def _package(root: Path) -> Path:
    """A synthetic offline package (no organiser data): every folder the entry point maps."""
    for rel in ("base", "enrichment", "models", "face-reference", "brand-stoplist"):
        (root / rel).mkdir(parents=True)
    (root / "base" / "dataset.json").write_text("{}", encoding="utf-8")
    (root / "enrichment" / "cache.json").write_text('{"cache": true}', encoding="utf-8")
    (root / "brand-stoplist" / "brand-stoplist.json").write_text("[]", encoding="utf-8")
    return root


def _run_env(fake_bin: Path, extra: dict, *args: str) -> dict:
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", **extra}
    result = subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_package_supplies_every_unset_setting(env_ftf, tmp_path):
    package = _package(tmp_path / "package")
    seen = _run_env(env_ftf, {"FTF_PACKAGE": str(package), "FTF_BASE_DIR": str(tmp_path / "none")}, "--help")
    assert seen["FTF_BASE"] == f"{package}/base/dataset.json"
    assert seen["FTF_MODEL_DIR"] == f"{package}/models"
    assert seen["FTF_FACE_REFERENCE"] == f"{package}/face-reference"
    assert seen["FTF_BRAND_STOPLIST"] == f"{package}/brand-stoplist/brand-stoplist.json"


def test_explicit_settings_win_over_the_package(env_ftf, tmp_path):
    package = _package(tmp_path / "package")
    extra = {
        "FTF_PACKAGE": str(package),
        "FTF_BASE_DIR": str(tmp_path / "none"),
        "FTF_BASE": "/elsewhere/dataset.json",
        "FTF_MODEL_DIR": "/elsewhere/models",
    }
    seen = _run_env(env_ftf, extra, "--help")
    assert seen["FTF_BASE"] == "/elsewhere/dataset.json"
    assert seen["FTF_MODEL_DIR"] == "/elsewhere/models"
    assert seen["FTF_FACE_REFERENCE"] == f"{package}/face-reference"


def test_package_folders_that_are_missing_are_left_unset(env_ftf, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    seen = _run_env(env_ftf, {"FTF_PACKAGE": str(package), "FTF_BASE_DIR": str(tmp_path / "none")}, "--help")
    assert all(seen[name] == "" for name in PACKAGE_VARS)


def test_missing_package_directory_is_not_an_error(env_ftf, tmp_path):
    seen = _run_env(env_ftf, {"FTF_PACKAGE": str(tmp_path / "absent"), "FTF_BASE_DIR": str(tmp_path / "none")}, "--help")
    assert seen["FTF_BASE"] == ""


def test_package_base_beats_the_base_folder(env_ftf, tmp_path):
    package = _package(tmp_path / "package")
    base = tmp_path / "base"
    base.mkdir()
    (base / "dataset.json").write_text("{}", encoding="utf-8")
    seen = _run_env(env_ftf, {"FTF_PACKAGE": str(package), "FTF_BASE_DIR": str(base)}, "--help")
    assert seen["FTF_BASE"] == f"{package}/base/dataset.json"


def test_serve_works_on_a_copy_of_the_package_cache_and_skips_the_build(env_ftf, tmp_path):
    package = _package(tmp_path / "package")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "case").mkdir()
    env = {
        "PATH": f"{env_ftf}{os.pathsep}{os.environ['PATH']}",
        "FTF_PACKAGE": str(package),
        "FTF_BASE_DIR": str(tmp_path / "none"),
        "FTF_RAW_ROOT": str(raw),
    }
    result = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True, check=True)
    assert "building it from" not in result.stderr
    cache_copy = Path("/tmp/ftf-enrichment/cache.json")
    try:
        assert f"FTF_ENRICH_CACHE={cache_copy}" in result.stdout
        assert cache_copy.read_text(encoding="utf-8") == '{"cache": true}'
        assert cache_copy != package / "enrichment" / "cache.json"
    finally:
        shutil.rmtree(cache_copy.parent, ignore_errors=True)
