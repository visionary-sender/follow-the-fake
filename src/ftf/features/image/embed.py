"""Image embeddings with SigLIP 2 (Apache-2.0) on CPU; torch and transformers load lazily.

Weights download on first use into ``model_dir()/hf``. Without torch/transformers or without
network on first use, ``get()`` returns None and the image family emits hashes only.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
from PIL import Image

from ftf.features.common import model_dir

log = logging.getLogger(__name__)

MODEL = "google/siglip2-base-patch16-224"
# Pinned snapshot so every run embeds with the same weights.
# Pinned snapshot, so stored vectors stay comparable; FTF_SIGLIP_REVISION overrides it.
REVISION = os.environ.get("FTF_SIGLIP_REVISION") or "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2" or None
# Tall page screenshots are cut to their top (at most MAX_ASPECT x width) before the model
# squashes them to 224x224: the first screen carries the hero image and headline.
MAX_ASPECT = 2.0


class Embedder:
    def __init__(self, threads: int):
        import torch
        from transformers import AutoImageProcessor, AutoModel

        torch.set_num_threads(threads)
        cache = model_dir() / "hf"
        self.torch = torch
        self.processor = AutoImageProcessor.from_pretrained(MODEL, revision=REVISION, cache_dir=cache)
        self.model = AutoModel.from_pretrained(MODEL, revision=REVISION, cache_dir=cache).eval()
        self.model_id = MODEL

    def __call__(self, rgb: Image.Image) -> tuple[np.ndarray, Optional[str]]:
        """L2-normalised vector and the crop locator (None when the whole image was used)."""
        locator = None
        w, h = rgb.size
        if h > MAX_ASPECT * w:
            rgb = rgb.crop((0, 0, w, int(MAX_ASPECT * w)))
            locator = f"crop=top:{int(MAX_ASPECT * w)}px"
        inputs = self.processor(images=[rgb], return_tensors="pt")
        with self.torch.inference_mode():
            out = self.model.get_image_features(pixel_values=inputs["pixel_values"])
        if not isinstance(out, self.torch.Tensor):  # some transformers versions return an output object
            out = out.pooler_output
        v = out[0].float().numpy()
        return v / (np.linalg.norm(v) or 1.0), locator


_EMBEDDER: Optional[Embedder] = None
_FAILED = False


def get() -> Optional[Embedder]:
    """Shared embedder, or None when disabled (``FTF_IMAGE_EMBED=0``) or unavailable."""
    global _EMBEDDER, _FAILED
    if _EMBEDDER is not None or _FAILED:
        return _EMBEDDER
    if os.environ.get("FTF_IMAGE_EMBED", "1") == "0":
        _FAILED = True
        return None
    try:
        _EMBEDDER = Embedder(threads=int(os.environ.get("FTF_THREADS", "4")))
    except Exception as e:  # missing extra, no network on first use, broken cache
        log.warning("image embeddings disabled: %s: %s", type(e).__name__, e)
        _FAILED = True
    return _EMBEDDER
