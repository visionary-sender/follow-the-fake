import shutil
import subprocess
import sys
import types

import pytest

from ftf.contract.models import Layer, Level
from ftf.features import pipeline, video
from ftf.features.common import Feature, dataset_from_files, read_dataset, write_dataset

pytest.importorskip("imagehash")
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def _make_video(path, with_audio=True):
    """Three visually different 2-second scenes (+ a sine tone), 320x240."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", "smptebars=s=320x240:d=2:r=10",
           "-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=10",
           "-f", "lavfi", "-i", "mandelbrot=s=320x240:r=10"]
    filt = "[2:v]trim=duration=2,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1:a=0[v]"
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:d=6"]
    cmd += ["-filter_complex", filt, "-map", "[v]"]
    if with_audio:
        cmd += ["-map", "3:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    (root / "case 5").mkdir(parents=True)
    (root / "case 14").mkdir()
    _make_video(root / "case 5" / "ad [123].mp4")
    shutil.copy(root / "case 5" / "ad [123].mp4", root / "case 14" / "ScreenRecording_01.MP4")
    (root / "case 5" / "broken.mp4").write_bytes(b"\x00" * 100)
    (root / "case 5" / "app.ts").write_text("export const x = 1;\n")
    return root


def test_probe_and_keyframes(raw, tmp_path):
    p = raw / "case 5" / "ad [123].mp4"
    info = video.probe(p)
    assert info.width == 320 and info.height == 240 and info.has_audio
    assert 5.5 < info.duration < 6.5
    frames = video.extract_keyframes(p, tmp_path / "kf", info)
    assert 3 <= len(frames) <= video.MAX_FRAMES
    ts = [k.t for k in frames]
    assert ts == sorted(ts) and ts[0] == 0.0
    # every scene is represented
    assert any(1.5 <= t < 2.5 for t in ts) and any(3.5 <= t < 4.5 for t in ts)
    assert all(k.path.parent == tmp_path / "kf" and k.path.exists() for k in frames)
    assert sorted(x.name for x in (tmp_path / "kf").iterdir()) == sorted(k.path.name for k in frames)
    th = [video._thumb(k.path) for k in frames]
    assert all(abs(a - b).mean() >= video.DEDUP_DIFF for i, a in enumerate(th) for b in th[:i])


def _lavfi(path, src):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_slow_change_is_covered_by_grid_and_still_video_dedups(tmp_path):
    slow = tmp_path / "slow.mp4"  # a gradual zoom never scores as a cut
    _lavfi(slow, "mandelbrot=s=160x120:r=10,trim=duration=20")
    ts = [k.t for k in video.extract_keyframes(slow, tmp_path / "a")]
    assert len(ts) >= 5 and max(ts) >= 15
    still = tmp_path / "still.mp4"
    _lavfi(still, "color=c=gray:s=160x120:r=10:d=20")
    assert [k.t for k in video.extract_keyframes(still, tmp_path / "b")] == [0.0]


def test_hostile_inputs_are_not_videos(raw, tmp_path):
    assert video.probe(raw / "case 5" / "broken.mp4") is None
    # a playlist renamed to .mp4 must not make ffprobe open other files
    evil = raw / "case 5" / "evil.mp4"
    evil.write_text(f"#EXTM3U\n#EXTINF:1,\nfile:{raw / 'case 5' / 'ad [123].mp4'}\n#EXT-X-ENDLIST\n")
    assert video.probe(evil) is None
    assert not video._is_ts(raw / "case 5" / "app.ts")


def test_apply_builtin_features(raw, tmp_path, monkeypatch):
    monkeypatch.setattr(video, "_still_families", lambda: [])
    ds = dataset_from_files(sorted(raw.rglob("*.*")), raw)
    out = tmp_path / "out"
    video.apply(ds, raw, out)
    by_path = {inc.source_ref: inc for inc in ds.incidents}

    ad = by_path["case 5/ad [123].mp4"]
    art = ad.captures[0].artefacts[0]
    kinds = [i.kind for i in ad.indicators]
    assert sorted(i.value for i in ad.indicators if i.kind == "video_meta") == [
        "duration_s=6.0", "resolution=320x240"]
    phash = [i for i in ad.indicators if i.kind == "image_phash"]
    assert len(phash) >= 3 and kinds.count("image_phash") == len(phash)
    for i in phash:
        assert i.provenance.artefact_id == art.id
        assert i.provenance.locator.startswith("t=") and i.provenance.locator.endswith("s")
        assert len(i.value) == 16 and int(i.value, 16) >= 0
        assert i.layer is Layer.creative and i.level is Level.campaign
        assert i.extractor == video.EXTRACTOR

    rec = by_path["case 14/ScreenRecording_01.MP4"]
    meta = [i for i in rec.indicators if i.kind in {"video_meta", "screen_size"}]
    assert [(i.kind, i.layer, i.value) for i in meta] == [("screen_size", Layer.collector, "320x240")]

    assert by_path["case 5/broken.mp4"].indicators == []
    assert by_path["case 5/app.ts"].indicators == []
    # keyframes live under the output directory, never next to the raw files
    assert not list(raw.rglob("*.jpg"))
    assert list((out / "keyframes").rglob("*.jpg"))
    doc = read_dataset(write_dataset(ds, out))
    assert sum(len(i.indicators) for i in doc.incidents) == sum(len(i.indicators) for i in ds.incidents)

    # re-running replaces the earlier output instead of duplicating it
    n = len(ad.indicators)
    video.apply(ds, raw, out)
    assert len(ad.indicators) == n


def test_still_families_get_keyframes(raw, tmp_path, monkeypatch):
    seen = []

    def features_for_image(path):
        seen.append(path)
        return [Feature(kind="image_dhash", value="ab"), Feature(kind="face_embedding",
                vector=[1.0, 0.0], model="fake/m", locator="face=0")]

    fake = types.ModuleType("ftf.features.image")
    fake.features_for_image = features_for_image
    monkeypatch.setitem(sys.modules, "ftf.features.image", fake)
    monkeypatch.setattr(video, "_still_families", lambda: [("image", fake)])
    ds = dataset_from_files([raw / "case 5" / "ad [123].mp4"], raw)
    video.apply(ds, raw, tmp_path / "out")
    inds = ds.incidents[0].indicators
    assert not [i for i in inds if i.kind == "image_phash"]  # stopgap off when image family exists
    dh = [i for i in inds if i.kind == "image_dhash"]
    fe = [i for i in inds if i.kind == "face_embedding"]
    assert len(dh) == len(fe) == len(seen) >= 3
    assert all(p.is_relative_to(tmp_path / "out" / "keyframes") for p in seen)
    assert fe[0].provenance.locator.startswith("t=") and fe[0].provenance.locator.endswith("s;face=0")
    assert fe[0].vector_ref.model == "fake/m"


def test_pipeline_discovers_video():
    assert "video" in pipeline.available_families()


def test_speech_optional(raw, monkeypatch):
    monkeypatch.delenv("FTF_VIDEO_SPEECH", raising=False)
    assert not video._speech_enabled()
    audio = video._audio(raw / "case 5" / "ad [123].mp4")
    assert audio is not None and abs(audio.size / 16000 - 6.0) < 0.2
    assert video._audio(raw / "case 5" / "broken.mp4") is None
