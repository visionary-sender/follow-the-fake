"""People and brands: faces matched against a reference set of impersonated public figures, and
the impersonated outlet or brand named in the creative or landing page.

Indicators (layer creative, level campaign, extractor ``faces@<version>``):

* ``face_embedding``: SFace 128-d vector for every detected face (model
  ``opencv_zoo/face_recognition_sface_2021dec``), so faces compare across captures without a
  reference. Locator ``face=<i>;bbox=x,y,w,h``, note ``det=<detector score>``.
* ``person_face``: the best-matching reference person for every face, whatever the similarity;
  note ``sface_cosine=<x>;top3=<name>:<cos>|...;det=<score>;ref=<manifest sha>``. No threshold
  is applied here: the linking engine calibrates (SFace's published same-person cosine is 0.363).
* ``brand``: an outlet, bank, company or institution named in OCR text, page text or the saved
  page's ``<title>``/og tags; one per capture and brand, locator lists the sources.

Heavy imports (cv2, PIL) happen inside functions, so importing this package needs numpy only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from ftf.contract.models import ArtefactType, Dataset
from ftf.features.common import (
    IMAGE_SUFFIXES,
    IMAGE_TYPES,
    Feature,
    IndicatorWriter,
    iter_artefacts,
    resolve,
)
from ftf.features.faces.brands import brands_in, html_title_text

__all__ = ["apply", "features_for_image", "EXTRACTOR"]

VERSION = "0.1"
EXTRACTOR = f"faces@{VERSION}"
TEXT_KINDS = ("ocr_text", "page_text")
HTML_TYPES = frozenset({ArtefactType.html, ArtefactType.mhtml})
OCR_TYPES = frozenset({ArtefactType.ad_screenshot, ArtefactType.screenshot})
OCR_MIN_BYTES = 10_000  # icons and tiny assets carry no outlet name worth an OCR pass

_engine_cache = None


def _engine():
    global _engine_cache
    if _engine_cache is None:
        from ftf.features.faces.models import FaceEngine

        _engine_cache = FaceEngine()
    return _engine_cache


def _face_features(faces, ref) -> list[Feature]:
    from ftf.features.faces.models import EMBEDDING_MODEL

    out = []
    for i, f in enumerate(faces):
        loc = f"face={i};bbox={','.join(str(v) for v in f.box)}"
        out.append(
            Feature(
                kind="face_embedding",
                vector=f.embedding,
                model=EMBEDDING_MODEL,
                locator=loc,
                note=f"det={f.score:.3f}",
            )
        )
        if ref is None:
            continue
        top = ref.match(f.embedding, k=3)
        if not top:
            continue
        top3 = "|".join(f"{n}:{c:.4f}" for n, c in top)
        out.append(
            Feature(
                kind="person_face",
                value=top[0][0],
                locator=loc,
                note=f"sface_cosine={top[0][1]:.4f};top3={top3};det={f.score:.3f};ref={ref.manifest_sha}",
            )
        )
    return out


def _brand_features(found) -> list[Feature]:
    return [
        Feature(
            kind="brand",
            value=name,
            locator="src=" + ",".join(sorted({s.split(":", 1)[0] for s in sources})),
            note=f"category={brand.category};matches={' | '.join(sources)}"[:500],
        )
        for name, (brand, sources) in sorted(found.items())
    ]


def ocr_text(path: Path, timeout: int = 120) -> Optional[str]:
    """Fallback OCR with the tesseract binary (pol+eng) when the text family gave none."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, str(path), "stdout", "-l", "pol+eng"],
            capture_output=True,
            timeout=timeout,
            env={"OMP_THREAD_LIMIT": "1", "PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


def features_for_image(path: Path, text: Optional[str] = None, ocr: bool = True) -> list[Feature]:
    """Faces (embeddings and best reference match) plus brands from ``text`` or, if not given
    and ``ocr`` is set, from a tesseract pass over the image. Used for video keyframes too."""
    from ftf.features.faces.models import load_bgr
    from ftf.features.faces.reference import load_reference

    path = Path(path)
    feats = _face_features(_engine().faces(load_bgr(path)), load_reference())
    if text is None and ocr:
        text = ocr_text(path)
    if text:
        feats += _brand_features(brands_in([("ocr_text", text)]))
    return feats


def apply(ds: Dataset, raw_root: Path, out_dir: Path) -> Dataset:
    from ftf.features.faces.models import load_bgr
    from ftf.features.faces.reference import load_reference

    engine = _engine()
    ref = load_reference()
    w = IndicatorWriter(ds, out_dir, EXTRACTOR)
    stats: dict = {"images": 0, "faces": 0, "errors": [], "cpu_s": [], "wall_s": [], "ocr_fallback": 0}

    # Faces on every still image.
    for inc, cap, art in iter_artefacts(ds, IMAGE_TYPES, IMAGE_SUFFIXES):
        c0, t0 = time.process_time(), time.perf_counter()
        try:
            faces = engine.faces(load_bgr(resolve(raw_root, art)))
        except Exception as e:  # unreadable or hostile file: skip it, keep going
            stats["errors"].append(f"{art.id}: {type(e).__name__}: {e}"[:300])
            continue
        for feat in _face_features(faces, ref):
            w.add(inc, cap, art, feat)
        stats["images"] += 1
        stats["faces"] += len(faces)
        stats["cpu_s"].append(time.process_time() - c0)
        stats["wall_s"].append(time.perf_counter() - t0)

    # Brands per capture, from text indicators, saved-page titles and fallback OCR.
    for inc in ds.incidents:
        # (capture id, artefact id or None for whole-capture text) -> [(kind, text)]
        texts_of: dict[tuple[str, Optional[str]], list[tuple[str, str]]] = defaultdict(list)
        for ind in inc.indicators:
            if ind.kind in TEXT_KINDS and ind.value and not ind.extractor.startswith("faces@"):
                texts_of[(ind.capture_id, ind.provenance.artefact_id)].append((ind.kind, ind.value))
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            sources: list[tuple[str, str]] = list(texts_of.get((cap.id, None), []))
            first_art = {}
            for art in cap.artefacts:
                texts = list(texts_of.get((cap.id, art.id), []))
                if art.path:
                    texts += _own_texts(art, raw_root, has_ocr=any(k == "ocr_text" for k, _ in texts), stats=stats)
                for src, text in texts:
                    sources.append((src, text))
                    for name in brands_in([(src, text)]):
                        first_art.setdefault(name, art)
            for feat in _brand_features(brands_in(sources)):
                w.add(inc, cap, first_art.get(feat.value), feat)
    w.save()
    _write_stats(Path(out_dir), stats, ref)
    return ds


def _own_texts(art, raw_root: Path, has_ocr: bool, stats: dict) -> list[tuple[str, str]]:
    """Text this family reads itself: saved-page titles, and OCR when the text family gave none."""
    try:
        p = resolve(raw_root, art)
        if art.type in HTML_TYPES:
            title = html_title_text(p)
            return [("html_title", title)] if title else []
        if (
            art.type in OCR_TYPES
            and not has_ocr
            and p.suffix.lower() in IMAGE_SUFFIXES
            and p.stat().st_size >= OCR_MIN_BYTES
        ):
            t = ocr_text(p)
            if t:
                stats["ocr_fallback"] += 1
                return [("ocr", t)]
    except (OSError, ValueError) as e:
        stats["errors"].append(f"{art.id}: {type(e).__name__}: {e}"[:300])
    return []


def _write_stats(out_dir: Path, stats: dict, ref) -> None:
    cpu = sorted(stats.pop("cpu_s"))
    wall = sorted(stats.pop("wall_s"))

    def q(xs, p):
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3) if xs else None

    stats.update(
        cpu_s_per_image={"mean": round(sum(cpu) / len(cpu), 3) if cpu else None, "p50": q(cpu, 0.5), "p95": q(cpu, 0.95)},
        wall_s_per_image={"mean": round(sum(wall) / len(wall), 3) if wall else None, "p50": q(wall, 0.5), "p95": q(wall, 0.95)},
        reference=None if ref is None else {"manifest_sha": ref.manifest_sha, "people": len(ref.people), "images": len(ref.names)},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "faces-stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    if stats["errors"]:
        print(f"faces: {len(stats['errors'])} artefacts skipped (see faces-stats.json)", file=sys.stderr)
