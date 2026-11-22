"""The shared offline switch for the visual and content families.

``FTF_OFFLINE=1`` means no family touches the network: model loaders pass ``local_files_only``
and the weights must already be in ``FTF_MODEL_DIR`` (``ftf features prefetch`` fills it).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def offline() -> bool:
    return os.environ.get("FTF_OFFLINE") == "1"


def hub_file(repo: str, revision: Optional[str], filename: str, *, local: Optional[bool] = None) -> Path:
    """One file of a Hugging Face model, cached in ``model_dir()/hf`` at ``revision``.

    Downloaded on first use; ``local`` (default: ``FTF_OFFLINE=1``) only reads the cache."""
    from huggingface_hub import hf_hub_download

    from ftf.features.common import model_dir

    use_local = offline() if local is None else local
    return Path(
        hf_hub_download(
            repo, filename, revision=revision, cache_dir=model_dir() / "hf", local_files_only=use_local
        )
    )
