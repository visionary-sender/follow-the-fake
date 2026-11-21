"""Text family: OCR of screenshots, visible page text, name-masked templates, keywords, embeddings.

Indicators (layer ``creative``, level ``campaign``):

* ``ocr_text`` / ``page_text``: the text itself (one per screenshot; one per capture's main page,
  taken from saved HTML, else MHTML, else the HAR).
* ``text_template``: MinHash of the name-masked text (hex); the masked text is in the sidecar
  ``<out>/text/masked.json`` keyed by the source indicator id.
* ``text_embedding``: multilingual-e5 vector of the content text.
* ``person_mention``: people named (gazetteer, canonical form; or first-name + surname pairs).
* ``keyword`` and ``topic``: from a fixed scam lexicon.

Derived indicators point at their source text indicator with ``provenance.locator = "src=<id>"``.
Only numpy is imported here; bs4, Pillow, onnxruntime and tokenizers load on first use.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ftf.contract.models import ArtefactType, Dataset, Observed
from ftf.features.common import IMAGE_SUFFIXES, Feature, IndicatorWriter, resolve
from ftf.features.text import keywords as kw
from ftf.features.text import template as tpl
from ftf.features.text.mask import mask, tokens

EXTRACTOR = "text@0.1.0"
OCR_TYPES = frozenset({ArtefactType.ad_screenshot, ArtefactType.screenshot})
PAGE_PRIORITY = (ArtefactType.html, ArtefactType.mhtml, ArtefactType.har)
MIN_CHARS = 20  # shorter texts carry no template or embedding signal

log = logging.getLogger(__name__)


def cpu_seconds() -> float:
    """CPU time of this process (all threads) plus finished child processes (Tesseract)."""
    try:
        import resource  # POSIX only
    except ImportError:
        return time.process_time()
    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + ch.ru_utime + ch.ru_stime


class _Embeddings:
    """Lazy embedder that turns itself off (with one warning) if the model cannot load."""

    def __init__(self):
        self._e = None
        self.failed = False
        self.seconds: list[float] = []
        self.errors: list[str] = []

    def __call__(self, text: str):
        if self.failed:
            return None
        if self._e is None:
            try:
                from ftf.features.text.embed import get_embedder
                from ftf.features.text.ocr import threads

                self._e = get_embedder(threads=threads())
            except Exception as e:  # missing extra, no network on first use, broken cache
                log.warning("text embeddings disabled: %s", e)
                self.failed = True
                return None
        t0 = cpu_seconds()
        try:
            v = self._e.embed(text)
        except Exception as e:  # one bad input must not stop the run
            self.errors.append(f"embedding: {type(e).__name__}: {e}"[:300])
            return None
        self.seconds.append(cpu_seconds() - t0)
        return v

    @property
    def model(self) -> str | None:
        return self._e.spec.model_id if self._e else None


def derived_features(text: str, embed=None) -> tuple[list[Feature], str]:
    """Template, embedding, people, keywords and topic of a content text; also the masked text."""
    m = mask(text)
    feats = []
    sh = tpl.shingles(tokens(m.text))
    if sh:  # masked texts under SHINGLE tokens get no template: they would all look alike
        feats.append(
            Feature(
                kind="text_template",
                value=tpl.to_hex(tpl.minhash(sh)),
                note=f"minhash{tpl.NUM_PERM}x32 over masked word {tpl.SHINGLE}-shingles",
            )
        )
    if embed is not None:
        v = embed(text)
        if v is not None:
            feats.append(Feature(kind="text_embedding", vector=v, model=embed.model))
    feats += [Feature(kind="person_mention", value=p, note="gazetteer") for p in m.people]
    feats += [Feature(kind="person_mention", value=p, note="name pair, surface form") for p in m.other_names]
    feats += [Feature(kind="keyword", value=k) for k in kw.keywords(text)]
    t = kw.topic(text)
    if t:
        feats.append(Feature(kind="topic", value=t, note="lexicon rule"))
    return feats, m.text


_IMAGE_EMBED = _Embeddings()  # shared, so a failed model load is tried once per process


def features_for_image(path: Path) -> list[Feature]:
    """OCR one still image: ``ocr_text`` plus the derived text features (for video keyframes)."""
    from ftf.features.text.ocr import content_lines, ocr_lines

    lines = ocr_lines(Path(path))
    if not lines:
        return []
    content = "\n".join(content_lines(lines))
    out = [Feature(kind="ocr_text", value="\n".join(lines))]
    if len(content) >= MIN_CHARS:
        out += derived_features(content, _IMAGE_EMBED)[0]
    return out


def _page_artefact(cap):
    for t in PAGE_PRIORITY:
        for art in cap.artefacts:
            if art.type == t and art.path:
                return art
    return None


def apply(ds: Dataset, raw_root: Path, out_dir: Path) -> Dataset:
    from ftf.features.text.ocr import OcrUnavailable, content_lines, ocr_lines
    from ftf.features.text.page import page_text

    writer = IndicatorWriter(ds, out_dir, EXTRACTOR)
    embed = _Embeddings()
    masked: dict[str, dict] = {}
    stats = {"ocr_s": [], "page_s": [], "errors": [], "ocr_unavailable": False}

    for inc in ds.incidents:
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            seen: set[tuple[str, str]] = set()
            sources = []  # (artefact, kind, full text, content text, observed)
            for art in cap.artefacts:
                if art.type not in OCR_TYPES or not art.path or stats["ocr_unavailable"]:
                    continue
                if Path(art.path).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                t0 = cpu_seconds()
                try:
                    lines = ocr_lines(resolve(raw_root, art))
                except OcrUnavailable as e:
                    log.warning("OCR disabled: %s", e)
                    stats["ocr_unavailable"] = True
                    continue
                except Exception as e:  # unreadable or hostile image: skip this artefact
                    stats["errors"].append(f"{art.id}: {type(e).__name__}: {e}"[:300])
                    continue
                finally:
                    stats["ocr_s"].append(cpu_seconds() - t0)
                if lines:
                    sources.append((art, "ocr_text", "\n".join(lines), "\n".join(content_lines(lines)), Observed.static))
            art = _page_artefact(cap)
            if art is not None:
                t0 = cpu_seconds()
                try:
                    text = page_text(resolve(raw_root, art), art.type.value)
                except Exception as e:
                    stats["errors"].append(f"{art.id}: {type(e).__name__}: {e}"[:300])
                    text = ""
                stats["page_s"].append(cpu_seconds() - t0)
                if text:
                    obs = Observed.live if art.type == ArtefactType.har else Observed.static
                    sources.append((art, "page_text", text, text, obs))

            for art, kind, full, content, obs in sources:
                src = writer.add(inc, cap, art, Feature(kind=kind, value=full), obs)
                if len(content) < MIN_CHARS:
                    continue
                feats, masked_text = derived_features(content, embed)
                masked[src.id] = {"capture_id": cap.id, "artefact_id": art.id, "masked_text": masked_text}
                for f in feats:
                    if f.kind in ("person_mention", "keyword", "topic"):
                        if (f.kind, f.value) in seen:
                            continue
                        seen.add((f.kind, f.value))
                    f.locator = f"src={src.id}"
                    writer.add(inc, cap, art, f, obs)

    writer.save()
    d = Path(out_dir) / "text"
    d.mkdir(parents=True, exist_ok=True)
    side = d / "masked.json"
    old = json.loads(side.read_text()) if side.exists() else {}
    caps = {c.id for inc in ds.incidents for c in inc.captures}
    old = {k: v for k, v in old.items() if v.get("capture_id") not in caps}
    side.write_text(json.dumps({**old, **masked}, ensure_ascii=False, indent=1))
    (d / "stats.json").write_text(json.dumps(_summary(stats, embed), indent=1))
    return ds


def _summary(stats: dict, embed: _Embeddings) -> dict:
    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "extractor": EXTRACTOR,
        "embedding_model": embed.model,
        "ocr_items": len(stats["ocr_s"]),
        "ocr_cpu_s_mean": mean(stats["ocr_s"]),
        "page_items": len(stats["page_s"]),
        "page_cpu_s_mean": mean(stats["page_s"]),
        "embed_items": len(embed.seconds),
        "embed_cpu_s_mean": mean(embed.seconds),
        "ocr_unavailable": stats["ocr_unavailable"],
        "embeddings_disabled": embed.failed,
        "errors": stats["errors"] + embed.errors,
    }
