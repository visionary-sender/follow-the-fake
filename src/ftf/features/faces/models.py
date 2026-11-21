"""Face detection (YuNet) and recognition (SFace) from OpenCV Zoo, CPU only.

Weights download on first use into ``model_dir()/opencv_zoo`` and are checked against pinned
SHA-256 digests. cv2 and PIL are imported here only, never on the ``ftf.features`` import path.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.features.common import model_dir

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"


@dataclass(frozen=True)
class Weights:
    id: str
    url: str
    sha256: str
    size: int
    licence: str
    source: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]


YUNET = Weights(
    id="opencv_zoo/face_detection_yunet_2023mar",
    url=f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size=232_589,
    licence="MIT",
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
)
SFACE = Weights(
    id="opencv_zoo/face_recognition_sface_2021dec",
    url=f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    size=38_696_353,
    licence="Apache-2.0",
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface",
)

# The vector store of face embeddings is named after this model string.
EMBEDDING_MODEL = SFACE.id

USER_AGENT = "ftf-features/0.1 (research prototype)"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_weights(w: Weights) -> Path:
    """Path of the weights file, downloading and verifying it on first use."""
    d = model_dir() / "opencv_zoo"
    p = d / w.filename
    if p.exists() and p.stat().st_size == w.size:
        return p
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{w.filename}.part"
    req = urllib.request.Request(w.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    digest = _sha256(tmp)
    if digest != w.sha256:
        tmp.unlink()
        raise RuntimeError(f"{w.id}: sha256 {digest} != pinned {w.sha256}")
    os.replace(tmp, p)
    return p


@dataclass
class Face:
    box: tuple[int, int, int, int]  # x, y, w, h in original image pixels
    score: float  # detector confidence
    embedding: np.ndarray  # 128-d SFace feature, not normalised


MAX_PIXELS = 120_000_000  # refuse decompression bombs


def load_bgr(path: Path) -> np.ndarray:
    """Decode an image file as inert pixels (first frame for animations) into BGR uint8."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(path) as im:
        im.seek(0)
        rgb = np.asarray(im.convert("RGB"))
    return np.ascontiguousarray(rgb[:, :, ::-1])


class FaceEngine:
    """YuNet detection + SFace alignment and embedding.

    Wide images are scaled to ``width`` pixels; tall pages (full-page screenshots) are scanned in
    overlapping square tiles so faces keep their size. ``det_threshold`` is the detector's own
    confidence floor (what counts as a face), not a similarity threshold.
    """

    def __init__(
        self,
        det_threshold: float = 0.7,
        width: int = 1280,
        max_tiles: int = 40,
        threads: Optional[int] = None,
    ):
        import cv2

        cv2.setNumThreads(threads or int(os.environ.get("FTF_THREADS", "4")))
        self.cv2 = cv2
        self.width = width
        self.max_tiles = max_tiles
        self.det = cv2.FaceDetectorYN.create(
            str(fetch_weights(YUNET)), "", (320, 320), det_threshold, 0.3, 5000
        )
        self.rec = cv2.FaceRecognizerSF.create(str(fetch_weights(SFACE)), "")

    def _detect(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img)
        return faces if faces is not None else np.zeros((0, 15), np.float32)

    def faces(self, bgr: np.ndarray) -> list[Face]:
        h, w = bgr.shape[:2]
        scale = min(1.0, self.width / w)
        img = self.cv2.resize(bgr, (round(w * scale), round(h * scale))) if scale < 1 else bgr
        ih, iw = img.shape[:2]
        tile = max(iw, 320)
        step = max(tile - tile // 4, 1)
        tops = list(range(0, max(ih - tile, 0) + 1, step))
        if tops[-1] + tile < ih:
            tops.append(ih - tile)
        tops = tops[: self.max_tiles]
        found: list[tuple[np.ndarray, np.ndarray]] = []  # (detection in img coords, embedding)
        for top in tops:
            crop = img[top : top + tile]
            for row in self._detect(crop):
                full = row.copy()
                full[1] += top  # box y
                full[5:14:2] += top  # y of the 5 landmarks
                if any(_iou(full[:4], f[:4]) > 0.4 for f, _ in found):
                    continue  # same face seen in the overlap of two tiles
                emb = self.rec.feature(self.rec.alignCrop(crop, row)).ravel().astype(np.float32)
                found.append((full, emb))
        out = []
        for full, emb in found:
            x, y, bw, bh = (full[:4] / scale).round().astype(int).tolist()
            out.append(Face(box=(x, y, bw, bh), score=float(full[14]), embedding=emb))
        out.sort(key=lambda f: (f.box[1], f.box[0]))
        return out


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return float(inter / union) if union > 0 else 0.0
