"""Video family: keyframes from ad videos and screen recordings, plus optional speech-to-text.

For every video artefact the family

1. probes it with ffprobe (duration, resolution, audio present),
2. picks keyframes with ffmpeg scene-change detection on a 2 fps stream, tops them up with evenly
   spaced frames when a video has few cuts, drops near-identical frames (pHash) and caps the count,
3. writes the keyframes as JPEGs under ``out_dir/keyframes/`` (never next to the raw files),
4. runs every installed still-image family's ``features_for_image`` on each keyframe and attaches
   the features to the video's capture with ``provenance.locator = "t=<s>s"`` and
   ``provenance.artefact_id`` = the video artefact; a built-in 64-bit pHash (``image_phash``)
   stands in while the image family is not installed,
5. emits ``video_meta`` values (duration, resolution) for ad videos; for screen recordings the
   resolution is the collector's screen (``screen_size``, collector layer) and the duration is the
   collector's behaviour, so neither is emitted as a creative signal,
6. optionally (``FTF_VIDEO_SPEECH=1``) transcribes Polish speech with faster-whisper on CPU and
   emits ``speech_text``.

Videos are hostile input: ffmpeg and ffprobe run with a timeout, no stdin, only the ``file``
protocol, a container whitelist and a cap on the decoded duration. Nothing here is a similarity
threshold; the only threshold (``DEDUP_BITS``) decides which frames are worth extracting.

This module imports only the standard library and numpy at import time; Pillow, ImageHash and
faster-whisper are imported where they are used.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.contract.models import ArtefactType, Dataset, Layer, Level
from ftf.features.common import (
    VIDEO_SUFFIXES,
    Feature,
    IndicatorWriter,
    iter_artefacts,
    model_dir,
    resolve,
)

EXTRACTOR = "video@0.1"

# Keyframe selection (frame selection only; not evidence thresholds).
SAMPLE_FPS = 2.0  # scene detection runs on this many frames per second
SCENE = 0.3  # ffmpeg scene score that counts as a cut
MIN_FRAMES = 4  # top up with evenly spaced frames below this
MAX_FRAMES = 40  # keep at most this many keyframes per video
DEDUP_BITS = 4  # a frame within this pHash distance of a kept frame is dropped
MAX_SIDE = 1600  # longest side of a written keyframe (enough for OCR of phone recordings)
MAX_SECONDS = 1800  # decode at most this much of any video
TIMEOUT = 900  # seconds per ffmpeg call

# Containers ffmpeg may open: no playlists (hls, concat) that could pull in other files.
FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts"
# MPEG-TS segments saved by HLS players (e.g. ``output_000_….ts``) are video too; ``.ts`` is also
# TypeScript, so a file only counts when it starts with the TS sync byte.
TS_SUFFIXES = frozenset({".ts", ".m2ts"})

SPEECH_MODEL = os.environ.get("FTF_VIDEO_SPEECH_MODEL", "Systran/faster-whisper-small")
_SCREEN_RECORDING = re.compile(r"screen.?recording|nagrywanie.ekranu|rpreplay|screencast", re.I)


def _threads() -> int:
    return max(1, int(os.environ.get("FTF_THREADS", "4")))


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class Keyframe:
    t: float  # seconds from the start
    path: Path


def _input_args(path: Path) -> list[str]:
    return [
        "-protocol_whitelist", "file",
        "-format_whitelist", FORMATS,
        "-i", f"file:{path}",
    ]


def _run(cmd: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
    )


def probe(path: Path) -> Optional[VideoInfo]:
    """Duration, size and audio presence, or None if ffprobe cannot read a video stream."""
    cmd = [
        "ffprobe", "-v", "error", "-of", "json",
        "-show_entries", "format=duration:stream=codec_type,width,height,duration",
        "-protocol_whitelist", "file", "-format_whitelist", FORMATS, f"file:{path}",
    ]
    try:
        r = _run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or b"{}")
    except json.JSONDecodeError:
        return None
    streams = doc.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    if video is None:
        return None

    def num(x) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    duration = num((doc.get("format") or {}).get("duration")) or num(video.get("duration"))
    return VideoInfo(
        duration=duration,
        width=int(video["width"]),
        height=int(video.get("height") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _scale() -> str:
    m = MAX_SIDE
    return (
        f"scale='if(gte(iw,ih),min({m},iw),-2)':'if(gte(iw,ih),-2,min({m},ih))'"
    )


_PTS = re.compile(rb"\bn:\s*(\d+)\b.*?\bpts_time:\s*([0-9.]+)")


def _ffmpeg_frames(path: Path, vf: str, out: Path, prefix: str, limit: int) -> list[Keyframe]:
    """Run one ffmpeg frame-extraction pass; frames are matched to showinfo timestamps."""
    pattern = out / f"{prefix}_%04d.jpg"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", str(_threads()),
        "-t", str(MAX_SECONDS), *_input_args(path),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"{vf},showinfo,{_scale()}",
        "-fps_mode", "vfr", "-frames:v", str(limit), "-q:v", "3", str(pattern),
    ]
    try:
        r = _run(cmd)
    except subprocess.TimeoutExpired:
        r = None
    times = [float(m.group(2)) for m in _PTS.finditer(r.stderr)] if r is not None else []
    files = sorted(out.glob(f"{prefix}_*.jpg"))
    frames = [Keyframe(t=t, path=f) for t, f in zip(times, files)]
    for f in files[len(frames):]:  # a frame without a timestamp cannot be placed
        f.unlink()
    return frames


def _phash(path: Path):
    import imagehash
    from PIL import Image

    with Image.open(path) as im:
        return imagehash.phash(im.convert("RGB"))


def extract_keyframes(path: Path, out: Path, info: Optional[VideoInfo] = None) -> list[Keyframe]:
    """Scene-change keyframes of one video, deduplicated, between MIN and MAX_FRAMES (if the video
    has that many distinct frames), sorted by time. Replaces anything already in ``out``."""
    info = info or probe(path)
    if info is None:
        return []
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # The first sampled frame always counts; later frames when the picture changes.
    scene = f"fps={SAMPLE_FPS},select='eq(n\\,0)+gt(scene\\,{SCENE})'"
    frames = _ffmpeg_frames(path, scene, out, "s", 4 * MAX_FRAMES)
    if len(frames) < MIN_FRAMES and info.duration > 0:
        rate = MIN_FRAMES / min(info.duration, MAX_SECONDS)
        frames += _ffmpeg_frames(path, f"fps={rate:.6f}", out, "u", MIN_FRAMES + 1)
    frames.sort(key=lambda k: k.t)

    kept: list[tuple[Keyframe, object]] = []
    for k in frames:
        try:
            h = _phash(k.path)
        except Exception:
            k.path.unlink(missing_ok=True)
            continue
        if any(h - other <= DEDUP_BITS for _, other in kept):
            k.path.unlink()
            continue
        kept.append((k, h))
    if len(kept) > MAX_FRAMES:  # keep an evenly spread subset
        idx = set(np.linspace(0, len(kept) - 1, MAX_FRAMES).round().astype(int).tolist())
        for i, (k, _) in enumerate(kept):
            if i not in idx:
                k.path.unlink()
        kept = [kv for i, kv in enumerate(kept) if i in idx]
    result = []
    for k, _ in kept:  # stable names: time in milliseconds
        final = out / f"t{round(k.t * 1000):08d}.jpg"
        k.path.rename(final)
        result.append(Keyframe(t=k.t, path=final))
    return result


def _locator(t: float, inner: Optional[str] = None) -> str:
    loc = f"t={t:.2f}s"
    return f"{loc};{inner}" if inner else loc


def _builtin_image_features(path: Path) -> list[Feature]:
    """Stopgap while the image family (HACK-14) is not installed: a 64-bit pHash."""
    return [Feature(kind="image_phash", value=str(_phash(path)))]


def _still_families() -> list[tuple[str, object]]:
    """Installed still-image families that expose ``features_for_image``."""
    found = []
    for name in ("text", "image", "faces"):
        try:
            mod = importlib.import_module(f"ftf.features.{name}")
        except (ImportError, OSError) as e:
            if not (isinstance(e, ModuleNotFoundError) and e.name == f"ftf.features.{name}"):
                print(f"ftf features video: skipping family {name!r}: {e}", file=sys.stderr)
            continue
        if callable(getattr(mod, "features_for_image", None)):
            found.append((name, mod))
    return found


def _audio(path: Path) -> Optional[np.ndarray]:
    """Mono 16 kHz float32 audio decoded by our own sandboxed ffmpeg call."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", str(_threads()),
        "-t", str(MAX_SECONDS), *_input_args(path),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
    ]
    try:
        r = _run(cmd)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768.0


_speech_model = None


def transcribe(path: Path) -> Optional[tuple[str, float, float]]:
    """(text, audio seconds, CPU seconds) of the speech in a video, or None without audio.

    faster-whisper (MIT) with an MIT-licensed Whisper model, Polish, CPU int8."""
    global _speech_model
    audio = _audio(path)
    if audio is None or audio.size == 0:
        return None
    from faster_whisper import WhisperModel

    if _speech_model is None:
        _speech_model = WhisperModel(
            SPEECH_MODEL,
            device="cpu",
            compute_type="int8",
            cpu_threads=_threads(),
            download_root=str(model_dir() / "faster-whisper"),
        )
    t0 = time.process_time()
    segments, _ = _speech_model.transcribe(audio, language="pl", beam_size=5, vad_filter=True)
    text = " ".join(s.text.strip() for s in segments if s.text.strip())
    return text, audio.size / 16000.0, time.process_time() - t0


def _speech_enabled() -> bool:
    return os.environ.get("FTF_VIDEO_SPEECH", "").lower() in {"1", "true", "yes"}


def _is_ts(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(189)
    except OSError:
        return False
    return len(head) == 189 and head[0] == 0x47 and head[188] == 0x47


def _video_artefacts(ds: Dataset, raw_root: Path):
    for inc, cap, art in iter_artefacts(ds, set(ArtefactType)):
        suffix = Path(art.path).suffix.lower()
        if art.type is ArtefactType.video or suffix in VIDEO_SUFFIXES:
            yield inc, cap, art
        elif suffix in TS_SUFFIXES and _is_ts(resolve(raw_root, art)):
            yield inc, cap, art


def _slug(artefact_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in artefact_id)[:80]
    return f"{safe}-{hashlib.sha1(artefact_id.encode()).hexdigest()[:8]}"


def apply(ds: Dataset, raw_root: Path, out_dir: Path) -> Dataset:
    raw_root, out_dir = Path(raw_root), Path(out_dir)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("ftf features video: ffmpeg/ffprobe not installed, skipping videos", file=sys.stderr)
        return ds
    writer = IndicatorWriter(ds, out_dir, EXTRACTOR)
    families = _still_families()
    builtin = not any(name == "image" for name, _ in families)
    speech = _speech_enabled()
    for inc, cap, art in _video_artefacts(ds, raw_root):
        path = resolve(raw_root, art)
        info = probe(path)
        if info is None:
            print(f"ftf features video: {art.id}: no readable video stream", file=sys.stderr)
            continue
        if _SCREEN_RECORDING.search(Path(art.path).name):
            writer.add(inc, cap, art, Feature(
                kind="screen_size", layer=Layer.collector, level=Level.capture,
                value=f"{info.width}x{info.height}", note="screen recording",
            ))
        else:
            if info.duration > 0:
                writer.add(inc, cap, art, Feature(kind="video_meta", value=f"duration_s={info.duration:.1f}"))
            writer.add(inc, cap, art, Feature(kind="video_meta", value=f"resolution={info.width}x{info.height}"))

        for k in extract_keyframes(path, out_dir / "keyframes" / _slug(art.id), info):
            feats: list[Feature] = _builtin_image_features(k.path) if builtin else []
            for name, mod in families:
                try:
                    feats.extend(mod.features_for_image(k.path))
                except Exception as e:  # one family failing on one frame must not stop the run
                    print(f"ftf features video: {name} on {art.id} t={k.t:.2f}: {e}", file=sys.stderr)
            for f in feats:
                f.locator = _locator(k.t, f.locator)
                writer.add(inc, cap, art, f)

        if speech and info.has_audio:
            res = transcribe(path)
            if res and res[0]:
                text, secs, cpu = res
                writer.add(inc, cap, art, Feature(
                    kind="speech_text", value=text, locator=f"t=0.00-{secs:.2f}s",
                    note=f"model={SPEECH_MODEL} lang=pl",
                ))
                print(f"ftf features video: {art.id}: speech {secs:.0f}s audio, {cpu:.0f}s CPU", file=sys.stderr)
    writer.save()
    return ds
