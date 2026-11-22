#!/usr/bin/env python3
"""Container check: do the baked feature families load with the network off (HACK-80).

Run inside the jury image with --network none and FTF_OFFLINE unset (the image sets only the
Hugging Face offline variables):

    docker run --rm --network none -v "$PWD/scripts:/check:ro" --entrypoint python3 IMAGE -I /check/image_families_check.py

Prints one line per family: ok, empty (the reference set is absent, so faces are embedded but
not named), or fail. Never prints paths or file content. Exit 0 unless a family fails.
Run it inside the image: it imports the installed ftf package.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def image() -> str:
    from ftf.features.image import embed

    return "ok" if embed.get() is not None else "fail"


def text() -> str:
    from ftf.features.text.embed import get_embedder

    get_embedder()
    return "ok"


def faces() -> str:
    from ftf.features.faces import _engine_or_none

    return "ok" if _engine_or_none() is not None else "fail"


def reference() -> str:
    from ftf.features.faces.reference import load_reference

    ref_dir = Path(os.environ.get("FTF_FACES_REFERENCE", ""))
    if not (ref_dir / "rows.json").exists():
        return "empty"
    return "ok" if load_reference() is not None else "fail"


def ocr() -> str:
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=False)
    langs = set((out.stdout + out.stderr).split())
    return "ok" if {"pol", "eng"} <= langs else "fail"


def main() -> int:
    rows = {}
    for name, fn in (("image", image), ("text", text), ("faces", faces), ("reference", reference), ("ocr", ocr)):
        try:
            rows[name] = fn()
        except Exception as e:  # report the class only; the message may name a path
            rows[name] = f"fail ({type(e).__name__})"
    for name, status in rows.items():
        print(f"{name}: {status}")
    return 1 if any(s.startswith("fail") for s in rows.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
