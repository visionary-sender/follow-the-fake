"""Multilingual text embeddings on CPU: multilingual-e5 ONNX export via onnxruntime.

No torch: the tokenizer (``tokenizers``) and the ONNX graph are downloaded from the Hugging Face
Hub on first use into ``model_dir()/hf`` at a pinned revision, so later runs work offline.
Inputs get the e5 ``passage: `` prefix; output is the mask-weighted mean of the last hidden state,
L2-normalised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ftf.features.common import model_dir


@dataclass(frozen=True)
class ModelSpec:
    repo: str
    revision: str
    onnx_file: str
    dim: int
    max_tokens: int = 512

    @property
    def model_id(self) -> str:
        """Model string for ``Feature.model`` / ``VectorRef.model``."""
        return self.repo if self.onnx_file == "onnx/model.onnx" else f"{self.repo}:{self.onnx_file}"


MODELS = {
    "e5-small": ModelSpec(
        "intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "onnx/model.onnx", 384,
    ),
    "e5-base": ModelSpec(
        "intfloat/multilingual-e5-base", "d128750597153bb5987e10b1c3493a34e5a4502a",
        "onnx/model.onnx", 768,
    ),
}
DEFAULT = "e5-small"


def spec() -> ModelSpec:
    return MODELS[os.environ.get("FTF_TEXT_MODEL", DEFAULT)]


class Embedder:
    def __init__(self, s: ModelSpec, threads: int):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        cache = model_dir() / "hf"
        get = lambda f: hf_hub_download(s.repo, f, revision=s.revision, cache_dir=cache)  # noqa: E731
        self.spec = s
        self.tok = Tokenizer.from_file(get("onnx/tokenizer.json"))
        self.tok.enable_truncation(s.max_tokens)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(get(s.onnx_file), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def embed(self, text: str) -> np.ndarray:
        enc = self.tok.encode("passage: " + text)
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.sess.run(None, feed)[0][0]  # (tokens, dim)
        m = mask[0][:, None].astype(np.float32)
        v = (hidden * m).sum(0) / max(m.sum(), 1.0)
        return (v / (np.linalg.norm(v) or 1.0)).astype(np.float32)


@lru_cache(maxsize=2)
def get_embedder(name: str | None = None, threads: int = 4) -> Embedder:
    return Embedder(MODELS[name] if name else spec(), threads)
