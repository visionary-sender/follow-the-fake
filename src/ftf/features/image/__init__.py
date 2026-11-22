"""Image family: perceptual hashes and embeddings of ad creatives, page screenshots and assets.

Indicators (layer ``creative``, level ``campaign``):

* ``image_phash``, ``image_dhash``: 64-bit hex, on a hue-invariant luminance channel;
* ``image_pdq``: 256-bit hex PDQ (BT.601 luma, as in the reference, so not hue-invariant);
* ``image_embedding``: SigLIP 2 vector (when torch/transformers are installed).

Inputs: ``ad_screenshot``, ``screenshot`` and ``asset`` artefacts (any file that decodes as PNG,
JPEG, GIF, WebP or BMP; saved assets often have odd suffixes such as ``.pobrane``), plus image
responses recorded in ``har`` artefacts (observed ``live``, locator ``har_entry=<n>``). Identical
bytes are emitted once per capture. Distances and thresholds belong to the linking engine.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from pathlib import Path
from typing import Union

from ftf.contract.models import ArtefactType, Dataset, Observed
from ftf.features.common import Feature, IndicatorWriter, iter_artefacts, resolve
from ftf.features.image import embed, har
from ftf.features.image.hashing import SkipImage, hashes, load

__all__ = ["EXTRACTOR", "apply", "features_for_image", "features_for_bytes"]

EXTRACTOR = "image@0.1.0"
FILE_TYPES = (ArtefactType.ad_screenshot, ArtefactType.screenshot, ArtefactType.asset)
# Assets are embedded too; set FTF_IMAGE_EMBED=0 to skip all embeddings.
log = logging.getLogger(__name__)


MAX_FILE_BYTES = 50_000_000  # an image file larger than this is skipped unread

def _features(src: Union[Path, bytes], with_embedding: bool = True) -> list[Feature]:
    img = load(src)
    feats = [Feature(kind=k, value=v) for k, v in hashes(img.rgb).items()]
    emb = embed.get() if with_embedding else None
    if emb is not None:
        vec, locator = emb(img.rgb)
        feats.append(Feature(kind="image_embedding", vector=vec, model=emb.model_id, locator=locator))
    return feats


def features_for_image(path: Union[str, Path], with_embedding: bool = True) -> list[Feature]:
    """Hashes (and the embedding) of one still image, e.g. a video keyframe.

    Returns [] for inputs the skip rules exclude (not a raster image, too small, flat).
    """
    try:
        return _features(Path(path), with_embedding)
    except SkipImage:
        return []


def features_for_bytes(data: bytes, with_embedding: bool = True) -> list[Feature]:
    try:
        return _features(bytes(data), with_embedding)
    except SkipImage:
        return []


def apply(ds: Dataset, raw_root: Path, out_dir: Path) -> Dataset:
    writer = IndicatorWriter(ds, out_dir, EXTRACTOR)
    seen: dict[str, set[str]] = {}  # capture id -> sha256 of image bytes already emitted
    stats: Counter = Counter()

    def emit(inc, cap, art, data: bytes, observed: Observed, locator, note) -> None:
        digest = hashlib.sha256(data).hexdigest()
        done = seen.setdefault(cap.id, set())
        if digest in done:
            stats["duplicate_in_capture"] += 1
            return
        done.add(digest)
        try:
            feats = _features(data)
        except SkipImage as e:
            stats[e.reason] += 1
            return
        stats["hashed"] += 1
        for f in feats:
            if locator:
                f.locator = f"{locator};{f.locator}" if f.locator else locator
            f.note = note
            writer.add(inc, cap, art, f, observed=observed)

    for inc, cap, art in iter_artefacts(ds, FILE_TYPES):
        try:
            path = resolve(raw_root, art)
            if path.stat().st_size > MAX_FILE_BYTES:
                stats["too_large"] += 1
                continue
            data = path.read_bytes()
        except (OSError, ValueError):
            stats["unreadable"] += 1
            continue
        emit(inc, cap, art, data, Observed.static, None, None)

    for inc, cap, art in iter_artefacts(ds, [ArtefactType.har]):
        try:
            path = resolve(raw_root, art)
        except ValueError:
            stats["unreadable"] += 1
            continue
        for i, url, body in har.iter_images(path):
            emit(inc, cap, art, body, Observed.live, f"har_entry={i}", url[:300])

    writer.save()
    log.info("image family: %s", dict(stats))
    return ds
