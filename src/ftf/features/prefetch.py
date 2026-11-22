"""Model weights for offline runs: ``ftf features prefetch`` fills ``FTF_MODEL_DIR``.

Parts (each is one file, shown with its size):

* text: ``intfloat/multilingual-e5-small`` ONNX graph and tokenizer (``text.embed.spec()``);
* image: ``google/siglip2-base-patch16-224`` processor config and weights at the pinned revision;
* faces: YuNet and SFace weights (``faces.models``);
* video (optional): the faster-whisper speech model, only with ``--speech`` or ``FTF_VIDEO_SPEECH=1``.

The face reference set is not part of prefetch (it lives in its own bucket). With
``FTF_OFFLINE=1`` and no reference directory the faces family still emits ``face_embedding``
and ``brand``, just no ``person_face``.

``missing(families)`` checks the same parts with no network, so ``ftf features`` under
``FTF_OFFLINE=1`` can name what is absent before it starts. Downloaders are imported inside the
functions, so importing this module needs only numpy.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ftf.features.common import model_dir
from ftf.features.offline import hub_file, offline

IMAGE_REPO = "google/siglip2-base-patch16-224"
# Files AutoImageProcessor and AutoModel read for this repo; checked on a VM.
IMAGE_FILES = ("config.json", "preprocessor_config.json", "model.safetensors")


@dataclass(frozen=True)
class Part:
    name: str
    family: str
    fetch: Callable[[], list[Path]]  # downloads (network allowed) and returns the files
    present: Callable[[], bool]  # local-only check, never touches the network


@dataclass(frozen=True)
class Item:
    name: str
    size: int  # bytes


def _size(path: Path) -> int:
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size


def _present(check: Callable[[], object]) -> bool:
    try:
        check()
    except Exception:  # not cached (or a broken cache entry): the caller reports it as missing
        return False
    return True


def _hub_part(family: str, repo: str, revision: Optional[str], filename: str) -> Part:
    rev = (revision or "main")[:7]
    return Part(
        name=f"{family}: {repo}@{rev} {filename}",
        family=family,
        fetch=lambda: [hub_file(repo, revision, filename, local=False)],
        present=lambda: _present(lambda: hub_file(repo, revision, filename, local=True)),
    )


def _text_parts() -> list[Part]:
    from ftf.features.text.embed import spec

    s = spec()
    return [_hub_part("text", s.repo, s.revision, f) for f in (s.onnx_file, "onnx/tokenizer.json")]


def _image_parts() -> list[Part]:
    from ftf.features.image.embed import REVISION

    return [_hub_part("image", IMAGE_REPO, REVISION, f) for f in IMAGE_FILES]


def _face_weight_parts() -> list[Part]:
    from ftf.features.faces import models

    def present(w) -> Callable[[], bool]:
        def check() -> bool:
            p = model_dir() / "opencv_zoo" / w.filename
            return p.exists() and p.stat().st_size == w.size

        return check

    return [
        Part(
            name=f"faces: {w.id} ({w.licence})",
            family="faces",
            fetch=lambda w=w: [models.fetch_weights(w)],
            present=present(w),
        )
        for w in (models.YUNET, models.SFACE)
    ]


def _speech_part() -> Part:
    from ftf.features.video import SPEECH_MODEL

    def root() -> str:
        return str(model_dir() / "faster-whisper")

    def fetch() -> list[Path]:
        from faster_whisper.utils import download_model

        return [Path(download_model(SPEECH_MODEL, cache_dir=root()))]

    def check() -> None:
        from faster_whisper.utils import download_model

        download_model(SPEECH_MODEL, cache_dir=root(), local_files_only=True)

    return Part(
        name=f"video: speech {SPEECH_MODEL}",
        family="video",
        fetch=fetch,
        present=lambda: _present(check),
    )


def _speech_enabled() -> bool:
    return os.environ.get("FTF_VIDEO_SPEECH", "").lower() in {"1", "true", "yes"}


def parts(families: Optional[Iterable[str]] = None, *, speech: bool = False) -> list[Part]:
    """Every weight part the given families load (all four when ``families`` is None).

    The speech model is included with ``speech=True`` or when ``FTF_VIDEO_SPEECH`` is on."""
    wanted = set(families) if families is not None else {"text", "image", "faces", "video"}
    out: list[Part] = []
    if "text" in wanted:
        out += _text_parts()
    if "image" in wanted and os.environ.get("FTF_IMAGE_EMBED", "1") != "0":
        out += _image_parts()
    if "faces" in wanted:
        out += _face_weight_parts()
    if "video" in wanted and (speech or _speech_enabled()):
        out.append(_speech_part())
    return out


@contextlib.contextmanager
def _model_dir_env(dest: Optional[Path]) -> Iterator[None]:
    if dest is None:
        yield
        return
    old = os.environ.get("FTF_MODEL_DIR")
    os.environ["FTF_MODEL_DIR"] = str(dest)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("FTF_MODEL_DIR", None)
        else:
            os.environ["FTF_MODEL_DIR"] = old


def prefetch(
    dest: Optional[Path] = None,
    *,
    speech: bool = False,
    out: Callable[[str], None] = print,
) -> list[Item]:
    """Download every weight part into ``dest`` (default ``FTF_MODEL_DIR``); print each with its
    size and the total."""
    if offline():
        raise SystemExit("ftf features prefetch downloads the models: unset FTF_OFFLINE first")
    items: list[Item] = []
    with _model_dir_env(dest):
        target = model_dir()
        for part in parts(speech=speech):
            size = sum(_size(p) for p in part.fetch())
            items.append(Item(part.name, size))
            out(f"{part.name:<78} {size / 2**20:9.1f} MB")
        total = sum(i.size for i in items)
        out(f"total {total / 2**20:.1f} MB ({len(items)} items) in {target}")
    return items


def missing(families: Iterable[str]) -> list[str]:
    """Names of the weight parts the families need that are not in ``model_dir()``; never downloads."""
    return [p.name for p in parts(list(families)) if not p.present()]
