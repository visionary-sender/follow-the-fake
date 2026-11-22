"""`ftf features prefetch` and the FTF_OFFLINE switch. No network: every downloader is replaced."""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("huggingface_hub")
pytest.importorskip("PIL")

import huggingface_hub  # noqa: E402

from ftf.features import commands, prefetch  # noqa: E402
from ftf.features.faces import models as face_models  # noqa: E402
from ftf.features.offline import hub_file  # noqa: E402


@pytest.fixture(autouse=True)
def model_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_MODEL_DIR", str(tmp_path / "models"))
    for var in ("FTF_OFFLINE", "FTF_VIDEO_SPEECH", "FTF_IMAGE_EMBED"):
        monkeypatch.delenv(var, raising=False)

    def no_network(*a, **k):
        raise AssertionError("network used by a test that must not reach it")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)


@pytest.fixture
def fake_hub(monkeypatch):
    calls: list[tuple[str, str, bool]] = []

    def hf_hub_download(repo_id, filename, *, revision=None, cache_dir=None, local_files_only=False, **kw):
        calls.append((repo_id, filename, local_files_only))
        p = Path(cache_dir) / "models--fake" / "snapshots" / (revision or "main") / filename
        if not p.exists():
            if local_files_only:
                raise FileNotFoundError(filename)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 1000)
        return str(p)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    return calls


@pytest.fixture
def fake_weights(monkeypatch):
    def fetch_weights(w):
        p = prefetch.model_dir() / "opencv_zoo" / w.filename
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.truncate(w.size)  # right size is what the presence check accepts
        return p

    monkeypatch.setattr(face_models, "fetch_weights", fetch_weights)


def test_prefetch_fills_every_weight_part_and_reports_sizes(fake_hub, fake_weights, tmp_path):
    lines: list[str] = []
    items = prefetch.prefetch(tmp_path / "fresh", out=lines.append)

    names = [i.name for i in items]
    assert any(n.startswith("text:") and "onnx/tokenizer.json" in n for n in names)
    assert any(n.startswith("image:") and "model.safetensors" in n for n in names)
    assert sum(n.startswith("faces:") for n in names) == 2  # YuNet and SFace only
    assert not any("reference" in n for n in names)  # the reference set is kept out of prefetch
    assert not any(n.startswith("video:") for n in names)  # speech only with --speech
    assert len(items) == 7
    assert all(i.size > 0 for i in items)
    total = sum(i.size for i in items)
    assert lines[-1].startswith(f"total {total / 2**20:.1f} MB (7 items)")
    assert all(not local for _, _, local in fake_hub)  # downloads, never local-only


def test_prefetch_refuses_under_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("FTF_OFFLINE", "1")
    with pytest.raises(SystemExit, match="unset FTF_OFFLINE"):
        prefetch.prefetch(tmp_path / "x")


def test_missing_names_absent_parts_until_prefetch_fills_them(fake_hub, fake_weights):
    before = prefetch.missing(["text", "image", "faces"])
    assert len(before) == 7  # 2 text files, 3 image files, 2 face weights
    assert any("onnx/tokenizer.json" in n for n in before)

    prefetch.prefetch(None, out=lambda s: None)
    assert prefetch.missing(["text", "image", "faces"]) == []


def test_offline_hub_files_come_from_cache_only(fake_hub, monkeypatch):
    monkeypatch.setenv("FTF_OFFLINE", "1")
    with pytest.raises(FileNotFoundError):
        hub_file("org/model", "abc123", "onnx/model.onnx")
    assert fake_hub == [("org/model", "onnx/model.onnx", True)]


def test_speech_part_only_with_flag_or_env(monkeypatch):
    assert [p.name for p in prefetch.parts(["video"])] == []
    [speech] = prefetch.parts(["video"], speech=True)
    assert speech.name.startswith("video: speech") and "Systran" in speech.name
    monkeypatch.setenv("FTF_VIDEO_SPEECH", "1")
    assert len(prefetch.parts(["video"])) == 1


def test_offline_run_names_the_missing_models(tmp_path, monkeypatch, fake_hub):
    monkeypatch.setenv("FTF_OFFLINE", "1")
    args = argparse.Namespace(
        action=None, out=tmp_path / "out", families="text", dataset=None,
        files=[tmp_path / "x.png"], raw_root=tmp_path, speech=False,
    )
    with pytest.raises(SystemExit, match="ftf features prefetch"):
        commands.run(args)


def test_cli_parses_prefetch_and_extraction_forms():
    root = argparse.ArgumentParser()
    commands.register(root.add_subparsers(dest="cmd"))

    pf = root.parse_args(["features", "prefetch", "--model-dir", "/x", "--speech"])
    assert (pf.action, pf.model_dir, pf.speech) == ("prefetch", Path("/x"), True)

    ext = root.parse_args(["features", "--files", "a.png", "--out", "o"])
    assert ext.action is None and ext.out == Path("o")

    with pytest.raises(SystemExit, match="--out is required"):
        commands.run(root.parse_args(["features", "--files", "a.png"]))
