"""Shared plumbing for the visual and content feature extractors (text, image, faces, video).

Each extractor family exposes:

* ``features_for_image(path) -> list[Feature]`` (where it applies to a still image, so the
  video family can run it on keyframes), and
* ``apply(ds, raw_root, out_dir) -> Dataset``: adds that family's indicators to every relevant
  artefact of a contract ``Dataset`` and writes vectors under ``out_dir/vectors/``.

Similarities are exported as raw inputs (hashes as values, embeddings as vectors); turning them
into evidence is the linking engine's job, so no thresholds live here.

Captured files are hostile: read them as inert bytes, never execute or render them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.contract.models import (
    Artefact,
    ArtefactType,
    Capture,
    Dataset,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
    Stage,
    VectorRef,
)

IMAGE_TYPES = frozenset({ArtefactType.ad_screenshot, ArtefactType.screenshot, ArtefactType.asset})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi"})


@dataclass
class Feature:
    """One extracted feature before it becomes a contract Indicator.

    Exactly one of ``value`` / ``vector`` is set. ``model`` names the vector's model and selects
    its store; ``locator`` says where inside the artefact (e.g. ``t=12.4s``, ``face=0``).
    """

    kind: str
    layer: Layer = Layer.creative
    level: Level = Level.campaign
    value: Optional[str] = None
    vector: Optional[np.ndarray] = None
    model: Optional[str] = None
    locator: Optional[str] = None
    note: Optional[str] = None


def model_dir() -> Path:
    """Where model weights are cached (``FTF_MODEL_DIR``, default ``~/.cache/ftf/models``)."""
    return Path(os.environ.get("FTF_MODEL_DIR", Path.home() / ".cache" / "ftf" / "models"))


def resolve(raw_root: Path, artefact: Artefact) -> Path:
    """Absolute path of an artefact, refusing paths that escape the raw root."""
    if not artefact.path:
        raise ValueError(f"artefact {artefact.id} has no path")
    root = raw_root.resolve()
    p = (root / artefact.path).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"artefact {artefact.id}: path escapes the raw root")
    return p


def iter_artefacts(
    ds: Dataset, types: Iterable[ArtefactType], suffixes: Optional[Iterable[str]] = None
) -> Iterator[tuple[Incident, Capture, Artefact]]:
    """Artefacts of the given types (and file suffixes), skipping exact-duplicate captures."""
    types = frozenset(types)
    suffixes = frozenset(s.lower() for s in suffixes) if suffixes else None
    for inc in ds.incidents:
        for cap in inc.captures:
            if cap.duplicate_of:
                continue
            for art in cap.artefacts:
                if art.type not in types or not art.path:
                    continue
                if suffixes is not None and Path(art.path).suffix.lower() not in suffixes:
                    continue
                yield inc, cap, art


class VectorStore:
    """Float32 vectors of one model: ``<out>/vectors/<slug>.npy`` plus ``<slug>.keys.json``.

    ``<slug>`` is the model name made filename-safe plus a short hash of the exact name.

    ``VectorRef.store`` is ``vectors/<slug>`` relative to the output directory and ``key`` is the
    indicator id. Embeddings are stored L2-normalised, so a dot product is the cosine.
    """

    def __init__(self, out_dir: Path, model: str):
        self.out_dir = Path(out_dir)
        self.model = model
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in model)
        self.slug = f"{safe}-{hashlib.sha1(model.encode()).hexdigest()[:8]}"  # distinct per model
        self.keys: list[str] = []
        self.rows: list[np.ndarray] = []

    @property
    def store(self) -> str:
        return f"vectors/{self.slug}"

    def add(self, key: str, vector: np.ndarray) -> VectorRef:
        v = np.asarray(vector, dtype=np.float32).ravel()
        if self.rows and v.shape != self.rows[0].shape:
            raise ValueError(f"{self.model}: dim {v.shape[0]} != {self.rows[0].shape[0]}")
        n = float(np.linalg.norm(v))
        self.keys.append(key)
        self.rows.append(v / n if n > 0 else v)
        return VectorRef(store=self.store, key=key, model=self.model, dim=int(v.shape[0]))

    def save(self) -> None:
        """Merge into an existing store of the same model (several families share one out_dir);
        rows with a key written again are replaced."""
        if not self.rows:
            return
        d = self.out_dir / "vectors"
        d.mkdir(parents=True, exist_ok=True)
        keys, rows = list(self.keys), np.stack(self.rows)
        if (d / f"{self.slug}.npy").exists():
            old_model = json.loads((d / f"{self.slug}.keys.json").read_text())["model"]
            if old_model != self.model:
                raise ValueError(f"vector store {self.slug} holds {old_model!r}, not {self.model!r}")
            old_keys, old_rows = load_vectors(self.out_dir, self.store)
            new = set(keys)
            keep = [i for i, k in enumerate(old_keys) if k not in new]
            if keep:
                keys = [old_keys[i] for i in keep] + keys
                rows = np.concatenate([old_rows[keep], rows])
        # Write both files under temporary names, then swap them in, so rows and keys stay aligned.
        tmp_npy, tmp_keys = d / f".{self.slug}.npy.tmp", d / f".{self.slug}.keys.json.tmp"
        with open(tmp_npy, "wb") as f:
            np.save(f, rows)
        tmp_keys.write_text(json.dumps({"model": self.model, "keys": keys}))
        os.replace(tmp_npy, d / f"{self.slug}.npy")
        os.replace(tmp_keys, d / f"{self.slug}.keys.json")


def load_vectors(out_dir: Path, store: str) -> tuple[list[str], np.ndarray]:
    """Keys and matrix of a store written by ``VectorStore.save``."""
    base = Path(out_dir) / store
    meta = json.loads(base.with_name(base.name + ".keys.json").read_text())
    return meta["keys"], np.load(base.with_name(base.name + ".npy"))


class IndicatorWriter:
    """Turns Features into Indicators with ids ``<capture_id>/<kind>/<n>`` (as in the fixture).

    Creating a writer removes indicators an earlier run of the same extractor (same name, any
    version) left in the dataset, so re-running a family replaces its output instead of
    duplicating it. Their vector rows are overwritten when the same ids are written again.
    """

    def __init__(self, ds: Dataset, out_dir: Path, extractor: str):
        self.ds = ds
        self.out_dir = Path(out_dir)
        self.extractor = extractor
        self.stores: dict[str, VectorStore] = {}
        name = extractor.split("@", 1)[0]
        for inc in ds.incidents:
            inc.indicators = [i for i in inc.indicators if i.extractor.split("@", 1)[0] != name]
        self.ids = {ind.id for inc in ds.incidents for ind in inc.indicators}

    def _next_id(self, capture_id: str, kind: str) -> str:
        n = 0
        while f"{capture_id}/{kind}/{n}" in self.ids:
            n += 1
        new = f"{capture_id}/{kind}/{n}"
        self.ids.add(new)
        return new

    def add(
        self,
        inc: Incident,
        cap: Capture,
        art: Optional[Artefact],
        feature: Feature,
        observed: Observed = Observed.static,
    ) -> Indicator:
        if (feature.value is None) == (feature.vector is None):
            raise ValueError(f"{feature.kind}: set exactly one of value / vector")
        store = None
        vector = None
        if feature.vector is not None:
            if not feature.model:
                raise ValueError(f"{feature.kind}: a vector feature needs a model")
            vector = np.asarray(feature.vector, dtype=np.float32).ravel()
            store = self.stores.setdefault(feature.model, VectorStore(self.out_dir, feature.model))
            if store.rows and vector.shape != store.rows[0].shape:
                raise ValueError(f"{feature.model}: dim {vector.shape[0]} != {store.rows[0].shape[0]}")
        ind_id = self._next_id(cap.id, feature.kind)
        vector_ref = (
            VectorRef(store=store.store, key=ind_id, model=feature.model, dim=int(vector.shape[0]))
            if store is not None
            else None
        )
        # Build (and so validate) the indicator before its vector is stored.
        try:
            ind = self._indicator(ind_id, cap, art, feature, vector_ref, observed)
        except Exception:
            self.ids.discard(ind_id)
            raise
        if store is not None:
            store.add(ind_id, vector)
        inc.indicators.append(ind)
        return ind

    def _indicator(self, ind_id, cap, art, feature, vector_ref, observed) -> Indicator:
        return Indicator(
            id=ind_id,
            capture_id=cap.id,
            layer=feature.layer,
            kind=feature.kind,
            level=feature.level,
            value=feature.value if vector_ref is None else None,
            vector_ref=vector_ref,
            observed=observed,
            provenance=Provenance(
                artefact_id=art.id if art else None, locator=feature.locator, note=feature.note
            ),
            extractor=self.extractor,
        )

    def save(self) -> None:
        for store in self.stores.values():
            store.save()


def write_dataset(ds: Dataset, out_dir: Path, name: str = "dataset.json") -> Path:
    """Validate and write the dataset document."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = Dataset.model_validate(ds.model_dump())
    p = out_dir / name
    p.write_text(doc.model_dump_json(exclude_none=True, indent=1))
    return p


def read_dataset(path: Path) -> Dataset:
    return Dataset.model_validate_json(Path(path).read_text())


def _artefact_type(path: Path) -> ArtefactType:
    s = path.suffix.lower()
    if s in IMAGE_SUFFIXES:
        return ArtefactType.screenshot
    if s in VIDEO_SUFFIXES:
        return ArtefactType.video
    if s in {".html", ".htm"}:
        return ArtefactType.html
    if s == ".har":
        return ArtefactType.har
    if s in {".mhtml", ".mht"}:
        return ArtefactType.mhtml
    if s == ".txt":
        return ArtefactType.text
    return ArtefactType.other


def dataset_from_files(paths: Iterable[Path], raw_root: Path, name: str = "dev-files") -> Dataset:
    """DEV ONLY until the ingestion adapters (HACK-9) land: one incident and capture per file.

    This is not a parser and must not grow into one; switch to the ingestion output once it exists.
    """
    root = raw_root.resolve()
    incidents = []
    for p in sorted({Path(x).resolve() for x in paths}):
        if not p.is_file():
            raise SystemExit(f"not a file: {p}")
        if not p.is_relative_to(root):
            raise SystemExit(f"{p} is outside the raw root {root}")
        rel = p.relative_to(root).as_posix()
        h = hashlib.sha1(rel.encode()).hexdigest()[:12]
        art = Artefact(id=f"dev/{h}/a0", type=_artefact_type(p), path=rel, size=p.stat().st_size)
        cap = Capture(id=f"dev/{h}", stage=Stage.unknown, artefacts=[art])
        incidents.append(Incident(id=f"dev/{h}/i", source="dev-files", source_ref=rel, captures=[cap]))
    return Dataset(name=name, incidents=incidents)


def add_io_args(parser: argparse.ArgumentParser) -> None:
    """Standard I/O flags shared by every feature subcommand."""
    parser.add_argument("--dataset", type=Path, help="input contract dataset JSON")
    parser.add_argument(
        "--files", type=Path, nargs="+", help="DEV ONLY: raw files to wrap one capture each"
    )
    parser.add_argument("--raw-root", type=Path, default=Path("/data/raw"))
    parser.add_argument("--out", type=Path, required=True, help="output directory")


def load_input(args: argparse.Namespace) -> Dataset:
    if bool(args.dataset) == bool(args.files):
        raise SystemExit("give exactly one of --dataset or --files")
    if args.dataset:
        return read_dataset(args.dataset)
    return dataset_from_files(args.files, args.raw_root)
