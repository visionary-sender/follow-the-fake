"""Image loading and perceptual hashes (pHash, dHash, PDQ) on a hue-invariant luminance channel.

Captured images are hostile bytes: only raster formats are decoded (never SVG), the size is
checked from the header before pixels are decoded, and animated images use their first frame.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import imagehash
import numpy as np
from PIL import Image

# Pillow decoders we allow; everything else (SVG, PDF, EPS, ...) is refused.
FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")

# Skip rules (not similarity thresholds): icons and tracking pixels below MIN_SIDE px on their
# shorter side, decompression bombs above MAX_PIXELS, and flat images (a blank frame hashes to
# a constant that would match every other blank frame).
MIN_SIDE = 40
MAX_PIXELS = 120_000_000
MIN_LUMA_STD = 2.0

# Luma weights of the CSS/SVG hue-rotate matrix (Filter Effects 1, feColorMatrix hueRotate).
# Every hue rotation keeps R*0.213 + G*0.715 + B*0.072 unchanged up to 8-bit clipping, so hashing
# this channel cancels the kits' random `filter: hue-rotate(Ndeg)` "uniqueizer".
CSS_LUMA = np.array([0.213, 0.715, 0.072], dtype=np.float32)


class SkipImage(Exception):
    """The input is not hashed; ``reason`` is one of the documented skip rules."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Loaded:
    rgb: Image.Image  # first frame, alpha composited on white, mode RGB
    width: int
    height: int
    format: str


def load(src: Union[str, Path, bytes]) -> Loaded:
    """Decode a raster image (path or bytes) or raise SkipImage."""
    fp = io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else open(src, "rb")
    try:
        try:
            img = Image.open(fp, formats=FORMATS)
        except (Image.UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
            raise SkipImage("not_raster") from e
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise SkipImage("too_large")
        if min(w, h) < MIN_SIDE:
            raise SkipImage("too_small")
        fmt = img.format or "?"
        try:
            img.seek(0)
            if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(bg, rgba).convert("RGB")
            else:
                rgb = img.convert("RGB")
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            raise SkipImage("decode_error") from e
        return Loaded(rgb=rgb, width=w, height=h, format=fmt)
    finally:
        fp.close()


def luma(rgb: Image.Image, weights: np.ndarray = CSS_LUMA) -> Image.Image:
    """Single-channel 8-bit image from RGB with the given weights."""
    a = np.asarray(rgb, dtype=np.float32)
    y = a @ weights
    return Image.fromarray(np.clip(np.rint(y), 0, 255).astype(np.uint8), mode="L")


def hue_rotate(rgb: Image.Image, degrees: float) -> Image.Image:
    """CSS ``filter: hue-rotate(<degrees>deg)`` in sRGB, for robustness tests."""
    c, s = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    m = np.array(
        [
            [0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928],
            [0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283],
            [0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072],
        ],
        dtype=np.float32,
    )
    a = np.asarray(rgb.convert("RGB"), dtype=np.float32) @ m.T
    return Image.fromarray(np.clip(np.rint(a), 0, 255).astype(np.uint8), mode="RGB")


def is_flat(y: Image.Image) -> bool:
    thumb = np.asarray(y.resize((64, 64), Image.Resampling.BILINEAR), dtype=np.float32)
    return float(thumb.std()) < MIN_LUMA_STD


def phash(y: Image.Image) -> str:
    return str(imagehash.phash(y, hash_size=8))


def dhash(y: Image.Image) -> str:
    return str(imagehash.dhash(y, hash_size=8))


# --- PDQ (Meta ThreatExchange, BSD) re-implemented in numpy so the container needs no compiler.
# Follows pdqhashing.cpp: luma (BT.601) -> 2 passes of a Jarosz box filter -> 64x64 decimation ->
# 16x16 DCT without the DC row/column -> bit = value > median. Hex is the reference format.

_PDQ_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float64)


def _dct_matrix() -> np.ndarray:
    i = np.arange(1, 17)[:, None]
    j = np.arange(64)[None, :]
    return np.sqrt(2.0 / 64.0) * np.cos((np.pi / 2 / 64.0) * i * (2 * j + 1))


_DCT = _dct_matrix()


def _window(old: int, new: int = 64) -> int:
    return (old + 2 * new - 1) // (2 * new)


def _box1d(a: np.ndarray, window: int) -> np.ndarray:
    """Centered running mean along the last axis with the reference's shrinking edges."""
    n = a.shape[-1]
    half = (window + 2) // 2
    c = np.concatenate([np.zeros(a.shape[:-1] + (1,)), np.cumsum(a, axis=-1)], axis=-1)
    out = np.empty_like(a)
    # Output index k averages input [k - (window - half), k + half - 1] clipped to the array.
    lo = np.clip(np.arange(n) - (window - half), 0, n)
    hi = np.clip(np.arange(n) + half, 0, n)
    out[...] = (c[..., hi] - c[..., lo]) / (hi - lo)
    return out


def pdq(rgb: Image.Image) -> tuple[str, int]:
    """256-bit PDQ hash (64 hex chars) and its quality score (0-100)."""
    a = np.asarray(rgb, dtype=np.float64) @ _PDQ_LUMA
    rows, cols = a.shape
    wr, wc = _window(cols), _window(rows)
    for _ in range(2):
        a = _box1d(a, wr)
        a = _box1d(a.T, wc).T
    ri = ((np.arange(64) + 0.5) * rows / 64).astype(int)
    ci = ((np.arange(64) + 0.5) * cols / 64).astype(int)
    b = a[np.ix_(ri, ci)]
    grad = np.sum((np.abs(np.diff(b, axis=0)) * 100 / 255).astype(int)) + np.sum(
        (np.abs(np.diff(b, axis=1)) * 100 / 255).astype(int)
    )
    quality = min(int(grad / 90), 100)
    d = _DCT @ b @ _DCT.T
    flat = d.ravel()
    bits = flat > np.median(flat)
    words = [int(sum(int(bits[w * 16 + k]) << k for k in range(16))) for w in range(16)]
    return "".join(f"{w:04x}" for w in reversed(words)), quality


def hamming(a: str, b: str) -> int:
    """Bit distance between two equal-length hex hashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hashes(rgb: Image.Image, weights: Optional[np.ndarray] = None) -> dict[str, str]:
    """``{kind: hex}`` for one decoded image; raises SkipImage for flat images."""
    y = luma(rgb, CSS_LUMA if weights is None else weights)
    if is_flat(y):
        raise SkipImage("flat")
    out = {"image_phash": phash(y), "image_dhash": dhash(y)}
    out["image_pdq"], _ = pdq(rgb)
    return out
