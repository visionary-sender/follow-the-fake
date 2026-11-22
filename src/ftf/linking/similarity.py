"""Calibrated similarity for continuous content views.

A *view* compares one kind of continuous indicator between two captures: cosine for embedding
vectors (`vector_ref` indicators), normalised Hamming similarity for perceptual-hash hex strings
and the MinHash / shingle Jaccard for signature values. A `Calibration`, fitted per view on
similarities of unrelated pairs (the null) and, when there are enough, of same-campaign pairs,
turns a similarity into bounded evidence in bits. `view_evidence` wraps both into a contract
`EvidenceRow`; it is what the ledger calls.

Calibration:

* With at least `MIN_POSITIVES` positive pairs: a 2-parameter logistic regression of
  same-campaign on the standardised similarity, with classes weighted equally so the training
  prior is removed (log LR = intercept + slope * z). The result is also bounded by the tail-LR
  bound log2(1 / P0(S >= s)), which holds for any monotone likelihood ratio, so a handful of
  positives cannot claim more than the null tail supports.
* With fewer positives: the tail-LR surrogate. The smoothed null tail p(s) = (k + 1) / (n + 1),
  k = number of null similarities >= s, is a valid p-value; it is turned into a likelihood
  ratio with the p-to-e calibrator kappa * p ** (kappa - 1) (expectation <= 1 under the null),
  so typical null similarities give 0 bits and the maximum grows only like log2(n_null).

Bits are always clipped to [0, cap]: a low similarity is weak evidence of nothing, never
evidence against a link, because creatives are routinely swapped inside a campaign.

Vectors (visual-features stage): `VectorRef.store` is used exactly as written in the
dataset, relative to that dataset's output directory, and read through
`ftf.features.common.load_vectors`. Vectors are L2-normalised; cosine is computed anyway, so an
unnormalised store is still compared correctly. A missing store or key reads as missing (0 bits).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence, Union

import numpy as np

from ftf.contract.models import Dataset, EvidenceRow, EvidenceStatus, Indicator, VectorRef, linkable
from ftf.features.common import load_vectors

CALIBRATION_FORMAT = "ftf-similarity-calibration@1"
MIN_POSITIVES = 10
DEFAULT_CAP_BITS = 4.0
DEFAULT_KAPPA = 0.5
_GRID_STEP = 1e-4  # null similarities are rounded up to this grid (conservative)
_EPS = 1e-9

# --------------------------------------------------------------------------- similarities


def cosine(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """Cosine similarity of two vectors; None for a zero, non-finite or mismatched vector."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if a.shape != b.shape or not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        return None
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return None
    return float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))


_HEX = re.compile(r"^(?:0x)?([0-9a-fA-F]+)$")


def _parse_hex(h: str) -> Optional[tuple[int, int]]:
    m = _HEX.match(h.strip())
    if not m:
        return None
    digits = m.group(1)
    return int(digits, 16), 4 * len(digits)


def hamming_similarity(a: str, b: str) -> Optional[float]:
    """1 - Hamming distance / bit length of two equal-length hex hashes; None otherwise."""
    pa, pb = _parse_hex(a), _parse_hex(b)
    if pa is None or pb is None or pa[1] != pb[1] or pa[1] == 0:
        return None
    return 1.0 - bin(pa[0] ^ pb[0]).count("1") / pa[1]


def _tokens(value: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", value.strip()) if t]


def minhash_similarity(a: str, b: str) -> Optional[float]:
    """Share of equal positions in two equal-length MinHash signatures (estimates Jaccard)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or len(ta) != len(tb):
        return None
    return sum(x == y for x, y in zip(ta, tb)) / len(ta)


def jaccard_similarity(a: str, b: str) -> Optional[float]:
    """Jaccard similarity of two token sets (e.g. shingle hashes)."""
    sa, sb = set(_tokens(a)), set(_tokens(b))
    if not sa or not sb:
        return None
    return len(sa & sb) / len(sa | sb)


_HEX_WORD = re.compile(r"^[0-9a-fA-F]+$")
TEMPLATE_WORDS = 128  # ftf.features.text.template.NUM_PERM


def hex_minhash_tokens(value: str) -> Optional[str]:
    """Visual's `text_template` value (one hex string of 128 big-endian uint32 words) as the
    space-separated tokens `minhash_similarity` reads; None unless it is exactly that format."""
    v = value.strip().lower()
    if len(v) != 8 * TEMPLATE_WORDS or not _HEX_WORD.match(v):
        return None
    return " ".join(v[i:i + 8] for i in range(0, len(v), 8))


def text_template_similarity(a: str, b: str) -> Optional[float]:
    """MinHash similarity of two Visual `text_template` values (name-masked text shingles)."""
    ta, tb = hex_minhash_tokens(a), hex_minhash_tokens(b)
    if ta is None or tb is None:
        return None
    return minhash_similarity(ta, tb)


# --------------------------------------------------------------------------- views


@dataclass(frozen=True)
class View:
    """A continuous view: which indicator kind it reads and how two values compare."""

    kind: str
    representation: str  # "vector" (Indicator.vector_ref) or "value" (Indicator.value)
    measure: str  # display name of the similarity, e.g. "cosine"
    lo: float  # range of the similarity
    hi: float
    similarity: Optional[Callable[[str, str], Optional[float]]] = None  # value views only
    multi_value: bool = False  # a value may hold several hashes (comma/space separated)


# Indicator kind -> view. Extend by adding a row (or `register_view`). Kinds follow the contract
# pattern ^[a-z][a-z0-9_]*$; embedding and perceptual-hash kinds come from the visual-features stage.
# Face embeddings get a view here, but a face match only says "same celebrity", which the scams
# share widely; its calibration null must include other scams with the same face.
VIEWS: dict[str, View] = {}


def register_view(view: View) -> View:
    if view.representation not in ("vector", "value"):
        raise ValueError(f"view {view.kind}: unknown representation {view.representation!r}")
    if view.representation == "value" and view.similarity is None:
        raise ValueError(f"view {view.kind}: value views need a similarity function")
    VIEWS[view.kind] = view
    return view


for _v in (
    View("text_embedding", "vector", "cosine", -1.0, 1.0),
    View("image_embedding", "vector", "cosine", -1.0, 1.0),
    View("image_phash", "value", "hamming", 0.0, 1.0, hamming_similarity, multi_value=True),
    View("face_embedding", "vector", "cosine", -1.0, 1.0),
    View("image_dhash", "value", "hamming", 0.0, 1.0, hamming_similarity, multi_value=True),
    View("image_pdq", "value", "hamming", 0.0, 1.0, hamming_similarity, multi_value=True),
    View("text_minhash", "value", "minhash", 0.0, 1.0, minhash_similarity),
    View("text_template", "value", "minhash", 0.0, 1.0, text_template_similarity),
    View("dom_minhash", "value", "minhash", 0.0, 1.0, minhash_similarity),
    View("dom_shingles", "value", "jaccard", 0.0, 1.0, jaccard_similarity),
):
    register_view(_v)


def view_for_kind(kind: str) -> Optional[View]:
    return VIEWS.get(kind)


# --------------------------------------------------------------------------- vector store


class VectorStore(Protocol):
    def get(self, ref: VectorRef, capture_id: Optional[str] = None) -> Optional[np.ndarray]:
        """The vector `ref` points at (for an indicator of `capture_id`), or None if missing."""


class DatasetVectorStore:
    """Vectors written by the features stage, resolved per dataset directory.

    `VectorRef.store` is relative to the output directory of the dataset the indicator came
    from. When a new incident is assigned against a linked collection, the collection's
    captures and the new incident's captures live in different directories, so the directory
    is looked up per capture: `capture_dirs` maps capture id -> directory, and `default_dir`
    covers captures not in the map. Each (directory, store) is loaded once through Visual's
    `ftf.features.common.load_vectors`; a missing or malformed store reads as missing.
    """

    def __init__(self, default_dir: Union[str, Path, None] = None,
                 capture_dirs: Optional[dict[str, Union[str, Path]]] = None,
                 extra_dirs: Sequence[Union[str, Path]] = ()):
        self.default_dir = Path(default_dir).resolve() if default_dir is not None else None
        self.capture_dirs = {c: Path(d).resolve() for c, d in (capture_dirs or {}).items()}
        # searched after the capture's own directory: Visual writes text and image vectors to
        # separate output directories, each with its own `vectors/` folder
        self.extra_dirs = [Path(d).resolve() for d in extra_dirs]
        self._stores: dict[tuple[Path, str], Optional[tuple[dict[str, int], np.ndarray]]] = {}

    @classmethod
    def for_datasets(cls, *datasets: tuple[Dataset, Union[str, Path]]) -> "DatasetVectorStore":
        """One store over several (dataset, its output directory) pairs; later pairs win."""
        dirs: dict[str, Union[str, Path]] = {}
        for ds, out_dir in datasets:
            for inc in ds.incidents:
                for cap in inc.captures:
                    dirs[cap.id] = out_dir
        default = datasets[0][1] if len(datasets) == 1 else None
        return cls(default, dirs)

    def add_dataset(self, ds: Dataset, out_dir: Union[str, Path]) -> None:
        d = Path(out_dir).resolve()
        for inc in ds.incidents:
            for cap in inc.captures:
                self.capture_dirs[cap.id] = d

    def dir_for(self, capture_id: Optional[str]) -> Optional[Path]:
        if capture_id is not None and capture_id in self.capture_dirs:
            return self.capture_dirs[capture_id]
        return self.default_dir

    def _load(self, base: Path, store: str) -> Optional[tuple[dict[str, int], np.ndarray]]:
        key = (base, store)
        if key not in self._stores:
            loaded = None
            target = (base / store).resolve()
            if base in target.parents:  # refuse store paths that leave the dataset directory
                try:
                    keys, matrix = load_vectors(base, store)
                    matrix = np.asarray(matrix)
                    if matrix.ndim == 2 and np.issubdtype(matrix.dtype, np.number) and len(keys) == len(matrix):
                        loaded = ({str(k): i for i, k in enumerate(keys)}, matrix)
                except (OSError, ValueError, KeyError, TypeError):
                    loaded = None
            self._stores[key] = loaded
        return self._stores[key]

    def get(self, ref: VectorRef, capture_id: Optional[str] = None) -> Optional[np.ndarray]:
        own = self.dir_for(capture_id)
        bases = list(dict.fromkeys(([own] if own is not None else []) + self.extra_dirs))
        for base in bases:
            loaded = self._load(base, ref.store)
            if loaded is None:
                continue
            index, matrix = loaded
            row = index.get(ref.key)
            if row is None:
                continue
            v = np.asarray(matrix[row], dtype=np.float64)
            if v.shape != (ref.dim,) or not np.all(np.isfinite(v)):
                continue
            return v
        return None


# --------------------------------------------------------------------------- calibration


def _f(x: Optional[float]) -> Optional[float]:
    return None if x is None or not math.isfinite(x) else round(float(x), 6)


def _auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """Mann-Whitney AUC with ties counted half."""
    allv = np.concatenate([pos, neg])
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(len(allv))
    sv = allv[order]
    i = 0
    while i < len(sv):  # average ranks over ties
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def cllr(llr_pos: np.ndarray, llr_neg: np.ndarray) -> float:
    """Cllr (van Leeuwen & Brümmer 2007, eq. 9) for natural-log LRs; 1.0 = uninformative."""
    c_pos = float(np.mean(np.logaddexp(0.0, -np.asarray(llr_pos, dtype=np.float64))))
    c_neg = float(np.mean(np.logaddexp(0.0, np.asarray(llr_neg, dtype=np.float64))))
    return (c_pos + c_neg) / (2.0 * math.log(2.0))


def cllr_min(pos: np.ndarray, neg: np.ndarray) -> float:
    """Cllr after the best monotone recalibration (pool-adjacent-violators)."""
    s = np.concatenate([pos, neg]).astype(np.float64)
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    order = np.argsort(s, kind="mergesort")
    ys = y[order]
    # PAV on ys (non-decreasing fit); tied similarities are pooled into one block first
    ss = s[order]
    blocks: list[list[float]] = []  # [sum_y, count]
    i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        blocks.append([float(ys[i : j + 1].sum()), float(j - i + 1)])
        i = j + 1
    merged: list[list[float]] = []
    for b in blocks:
        merged.append(b)
        while len(merged) > 1 and merged[-2][0] / merged[-2][1] >= merged[-1][0] / merged[-1][1]:
            top = merged.pop()
            merged[-1][0] += top[0]
            merged[-1][1] += top[1]
    post = np.concatenate([np.full(int(c), sy / c) for sy, c in merged])
    prior = len(pos) / len(s)
    with np.errstate(divide="ignore"):
        llr = np.log(post) - np.log1p(-post) - math.log(prior / (1.0 - prior))
    return cllr(llr[ys == 1], llr[ys == 0])


def _fit_logistic(z_pos: np.ndarray, z_neg: np.ndarray, ridge: float) -> tuple[float, float]:
    """Class-balanced 2-parameter logistic regression with an L2 penalty on the slope."""
    z = np.concatenate([z_pos, z_neg])
    y = np.concatenate([np.ones(len(z_pos)), np.zeros(len(z_neg))])
    w = np.concatenate([np.full(len(z_pos), 0.5 / len(z_pos)), np.full(len(z_neg), 0.5 / len(z_neg))])
    X = np.column_stack([np.ones_like(z), z])

    def loss(t: np.ndarray) -> float:
        eta = X @ t
        return float(np.sum(w * (np.logaddexp(0.0, eta) - y * eta)) + 0.5 * ridge * t[1] ** 2)

    t = np.zeros(2)
    cur = loss(t)
    for _ in range(100):
        p = 1.0 / (1.0 + np.exp(-(X @ t)))
        g = X.T @ (w * (p - y)) + np.array([0.0, ridge * t[1]])
        H = (X * (w * p * (1 - p))[:, None]).T @ X + np.diag([1e-12, ridge])
        step = np.linalg.solve(H, g)
        lam = 1.0
        while lam > 1e-6:
            nt = t - lam * step
            nl = loss(nt)
            if nl <= cur:
                break
            lam /= 2
        if cur - nl < 1e-12:
            t = nt if nl <= cur else t
            break
        t, cur = nt, nl
    return float(t[0]), float(t[1])


@dataclass
class Calibration:
    """Monotone, capped map from a view's similarity to bits of evidence for a link."""

    view: str
    method: str  # "logistic" or "tail_lr"
    cap_bits: float
    n_null: int
    n_pos: int
    lo: float
    hi: float
    # null tail: ascending similarities (rounded up to _GRID_STEP) and #null >= each of them
    knots: list[float]
    tail_counts: list[int]
    kappa: float = DEFAULT_KAPPA
    # logistic: log LR (nats) = intercept + slope * (s - z_mean) / z_scale
    intercept: Optional[float] = None
    slope: Optional[float] = None
    z_mean: Optional[float] = None
    z_scale: Optional[float] = None
    quality: dict[str, Any] = field(default_factory=dict)
    # face views only: True when the null was drawn from other scams showing the same face, so a
    # match beyond it means more than "same celebrity"; otherwise the ledger caps it low
    null_same_face: bool = False

    # ----------------------------------------------------------------- fitting

    @classmethod
    def fit(
        cls,
        null_sims: Iterable[float],
        pos_sims: Optional[Iterable[float]] = None,
        cap_bits: float = DEFAULT_CAP_BITS,
        *,
        view: str = "",
        min_positives: int = MIN_POSITIVES,
        kappa: float = DEFAULT_KAPPA,
        lo: Optional[float] = None,
        hi: Optional[float] = None,
    ) -> "Calibration":
        """Fit from similarities of unrelated pairs (null) and of same-campaign pairs (pos)."""
        null = np.asarray(list(null_sims), dtype=np.float64)
        null = null[np.isfinite(null)]
        pos = np.asarray(list(pos_sims) if pos_sims is not None else [], dtype=np.float64)
        pos = pos[np.isfinite(pos)]
        if len(null) == 0:
            raise ValueError("calibration needs at least one null similarity")
        if not (cap_bits > 0):
            raise ValueError("cap_bits must be positive")
        if not (0.0 < kappa < 1.0):
            raise ValueError("kappa must be in (0, 1)")
        v = VIEWS.get(view)
        lo = lo if lo is not None else (v.lo if v else -1.0)
        hi = hi if hi is not None else (v.hi if v else 1.0)

        grid = np.ceil(null / _GRID_STEP - _EPS) * _GRID_STEP
        knots, counts = np.unique(np.round(grid, 6), return_counts=True)
        tail = np.cumsum(counts[::-1])[::-1]
        cal = cls(view=view, method="tail_lr", cap_bits=float(cap_bits), n_null=int(len(null)),
                  n_pos=int(len(pos)), lo=float(lo), hi=float(hi), knots=[float(k) for k in knots],
                  tail_counts=[int(t) for t in tail], kappa=float(kappa))

        if len(pos) >= min_positives:
            mu = float(null.mean())
            sd = max(float(null.std()), 1e-3)
            a, b = _fit_logistic((pos - mu) / sd, (null - mu) / sd, ridge=1.0 / (2 * len(pos)))
            cal.method = "logistic"
            cal.intercept, cal.slope, cal.z_mean, cal.z_scale = a, b, mu, sd
        cal.quality = cal._quality(null, pos)
        return cal

    # ----------------------------------------------------------------- evaluation

    def _null_tail(self, s: np.ndarray) -> np.ndarray:
        """Number of (rounded-up) null similarities >= s."""
        knots = np.asarray(self.knots)
        tail = np.append(np.asarray(self.tail_counts, dtype=np.float64), 0.0)
        return tail[np.searchsorted(knots, s - _EPS, side="left")]

    def _tail_bound_bits(self, s: np.ndarray) -> np.ndarray:
        """log2(1 / p) with the smoothed tail p = (k + 1) / (n + 1): the most any monotone LR allows."""
        return np.log2((self.n_null + 1.0) / (self._null_tail(s) + 1.0))

    def bits_array(self, sims: Union[Sequence[float], np.ndarray]) -> np.ndarray:
        s = np.asarray(sims, dtype=np.float64)
        bound = self._tail_bound_bits(s)
        if self.method == "logistic":
            if self.slope is None or self.slope <= 0:
                raw = np.zeros_like(s)
            else:
                raw = (self.intercept + self.slope * (s - self.z_mean) / self.z_scale) / math.log(2.0)
            raw = np.minimum(raw, bound)
        else:
            # p-to-e calibrator: LR = kappa * p**(kappa - 1)  ->  bits = log2 kappa + (1 - kappa) * log2(1/p)
            raw = math.log2(self.kappa) + (1.0 - self.kappa) * bound
        out = np.clip(raw, 0.0, self.cap_bits)
        return np.where(np.isfinite(s), out, 0.0)

    def bits(self, sim: Optional[float]) -> float:
        if sim is None or not math.isfinite(sim):
            return 0.0
        return float(self.bits_array(np.array([sim]))[0])

    def sim_at_bits(self, target: float) -> Optional[float]:
        """Smallest similarity on the view's range (1e-4 grid) that earns at least `target` bits."""
        grid = np.round(np.arange(self.lo, self.hi + _GRID_STEP / 2, _GRID_STEP), 6)
        b = self.bits_array(grid)
        hit = np.nonzero(b >= target - 1e-9)[0]
        return float(grid[hit[0]]) if len(hit) else None

    def _quality(self, null: np.ndarray, pos: np.ndarray) -> dict[str, Any]:
        q: dict[str, Any] = {
            "view": self.view,
            "method": self.method,
            "n_null": self.n_null,
            "n_pos": self.n_pos,
            "cap_bits": self.cap_bits,
            "null_quantiles": {k: _f(float(np.quantile(null, p)))
                               for k, p in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("max", 1.0))},
            "max_bits": _f(float(self.bits_array(np.array([self.hi]))[0])),
            "sim_at_bits": {str(t): _f(self.sim_at_bits(t)) for t in (1, 2, 4)},
        }
        if self.method == "logistic":
            q["logistic"] = {"intercept": _f(self.intercept), "slope": _f(self.slope),
                             "z_mean": _f(self.z_mean), "z_scale": _f(self.z_scale)}
            if self.slope is not None and self.slope <= 0:
                q["warning"] = "non-positive slope: the view does not separate positives; it gives 0 bits"
        else:
            q["kappa"] = self.kappa
        if len(pos):
            ln2 = math.log(2.0)
            q["auc"] = _f(_auc(pos, null))
            q["cllr"] = _f(cllr(self.bits_array(pos) * ln2, self.bits_array(null) * ln2))
            q["cllr_min"] = _f(cllr_min(pos, null))
            q["note"] = "AUC and Cllr are in-sample (on the fitting pairs); Cllr < 1 means the view is informative"
        return q

    # ----------------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": CALIBRATION_FORMAT,
            "view": self.view,
            "method": self.method,
            "cap_bits": self.cap_bits,
            "n_null": self.n_null,
            "n_pos": self.n_pos,
            "lo": self.lo,
            "hi": self.hi,
            "kappa": self.kappa,
            "intercept": self.intercept,
            "slope": self.slope,
            "z_mean": self.z_mean,
            "z_scale": self.z_scale,
            "knots": self.knots,
            "tail_counts": self.tail_counts,
            "quality": self.quality,
            "null_same_face": self.null_same_face,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        if d.get("format") != CALIBRATION_FORMAT:
            raise ValueError(f"not a calibration document: format {d.get('format')!r}")
        if d["method"] not in ("logistic", "tail_lr"):
            raise ValueError(f"unknown calibration method {d['method']!r}")
        if len(d["knots"]) != len(d["tail_counts"]):
            raise ValueError("knots and tail_counts differ in length")
        return cls(**{k: v for k, v in d.items() if k != "format"})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: Union[str, bytes]) -> "Calibration":
        return cls.from_dict(json.loads(text))

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Calibration":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def save_calibrations(cals: dict[str, Calibration], path: Union[str, Path]) -> None:
    """Write several calibrations (keyed by view kind) to one JSON file."""
    doc = {"format": CALIBRATION_FORMAT, "views": {k: c.to_dict() for k, c in sorted(cals.items())}}
    Path(path).write_text(json.dumps(doc, indent=2), encoding="utf-8")


def load_calibrations(path: Union[str, Path]) -> dict[str, Calibration]:
    """Calibrations keyed by view kind, from `save_calibrations` (or `ftf calibrate-similarity`)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != CALIBRATION_FORMAT or not isinstance(doc.get("views"), dict):
        raise ValueError(f"{path}: not a calibration file")
    try:
        return {str(k): Calibration.from_dict(v) for k, v in doc["views"].items()}
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError(f"{path}: malformed calibration entry ({exc})") from None


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True)
class ViewMatch:
    """Best-pair comparison of one view between two captures."""

    kind: str
    similarity: Optional[float]  # None: one side has no usable value
    bits: float
    indicator_ids: tuple[str, ...]  # the best pair, or the present side when missing
    reason: Optional[str] = None  # why the similarity is missing


def _as_list(x: Union[Indicator, Sequence[Indicator], None]) -> list[Indicator]:
    if x is None:
        return []
    return [x] if isinstance(x, Indicator) else list(x)


def similarity(view: View, ind_a: Union[Indicator, Sequence[Indicator], None],
               ind_b: Union[Indicator, Sequence[Indicator], None],
               store: Optional[VectorStore] = None) -> tuple[Optional[float], tuple[str, ...], Optional[str]]:
    """Best similarity over all pairs of usable indicators of `view.kind` on each side."""
    a = [i for i in _as_list(ind_a) if i.kind == view.kind and linkable(i)]
    b = [i for i in _as_list(ind_b) if i.kind == view.kind and linkable(i)]
    present = tuple(i.id for i in a + b)
    if not a or not b:
        return None, present, "one side has no indicator of this kind"
    best: Optional[float] = None
    best_ids: tuple[str, ...] = present
    reason = "no comparable pair"
    if view.representation == "vector":
        if store is None:
            return None, present, "no vector store"
        va = [(i, i.vector_ref, store.get(i.vector_ref, i.capture_id)) for i in a if i.vector_ref is not None]
        vb = [(i, i.vector_ref, store.get(i.vector_ref, i.capture_id)) for i in b if i.vector_ref is not None]
        for ia, ra, xa in va:
            for ib, rb, xb in vb:
                if xa is None or xb is None:
                    reason = "vector not in store"
                    continue
                if ra.model != rb.model or ra.dim != rb.dim:
                    reason = "embedding models differ"
                    continue
                s = cosine(xa, xb)
                if s is not None and (best is None or s > best):
                    best, best_ids = s, (ia.id, ib.id)
    else:
        def values(i: Indicator) -> list[str]:
            if i.value is None:
                return []
            return _tokens(i.value) if view.multi_value else [i.value]

        for ia in a:
            for ib in b:
                for xa in values(ia):
                    for xb in values(ib):
                        s = view.similarity(xa, xb)
                        if s is not None and (best is None or s > best):
                            best, best_ids = s, (ia.id, ib.id)
    return best, best_ids, (None if best is not None else reason)


def compare_view(view: View, ind_a, ind_b, store: Optional[VectorStore], calibration: Calibration) -> ViewMatch:
    s, ids, reason = similarity(view, ind_a, ind_b, store)
    return ViewMatch(view.kind, s, calibration.bits(s) if s is not None else 0.0, ids, reason)


def view_evidence(view: View, ind_a: Union[Indicator, Sequence[Indicator], None],
                  ind_b: Union[Indicator, Sequence[Indicator], None],
                  store: Optional[VectorStore], calibration: Calibration) -> Optional[EvidenceRow]:
    """Ledger row for one content view between two captures.

    `shared` with the calibrated bits when the similarity earns any; `missing` (0 bits) when a
    side lacks a usable value; None when neither side has the view, or when both have it but the
    similarity earns no bits (the contract has no status for "compared, not similar").
    """
    a = [i for i in _as_list(ind_a) if i.kind == view.kind and linkable(i)]
    b = [i for i in _as_list(ind_b) if i.kind == view.kind and linkable(i)]
    if not a and not b:
        return None
    layer = (a + b)[0].layer
    m = compare_view(view, a, b, store, calibration)
    if m.similarity is None:
        return EvidenceRow(layer=layer, kind=view.kind, status=EvidenceStatus.missing, bits=0.0,
                           indicator_ids=list(m.indicator_ids), note=m.reason)
    if m.bits <= 0.0:
        return None
    note = (f"{view.measure} similarity {m.similarity:.3f}; {calibration.method} calibration "
            f"(n_null={calibration.n_null}, n_pos={calibration.n_pos}, cap {calibration.cap_bits:g} bits)")
    return EvidenceRow(layer=layer, kind=view.kind, status=EvidenceStatus.shared,
                       value=f"{view.measure} {m.similarity:.3f}", bits=round(m.bits, 3),
                       indicator_ids=list(m.indicator_ids), note=note)


# --------------------------------------------------------------------------- ledger switch

FACE_KIND = "face_embedding"
FACE_CAP_BITS = 1.0  # a face match says "same celebrity": scams of many operators share one face
OPERATOR_FACTOR = 0.5  # a similar creative says less about the operator than about the campaign


@dataclass
class ContentViews:
    """The content-view switch of the ledger: fitted calibrations plus where the vectors live.

    Only views with a calibration count; vector views also need a store. With no usable view the
    switch is off (`active()` empty) and the ledger scores exactly as without it.
    """

    calibrations: dict[str, Calibration]
    store: Optional[VectorStore] = None

    def active(self) -> list[tuple[View, Calibration]]:
        out = []
        for kind in sorted(self.calibrations):
            view = VIEWS.get(kind)
            if view is None or (view.representation == "vector" and self.store is None):
                continue
            out.append((view, self.calibrations[kind]))
        return out

    def cap(self, kind: str, calibration: Calibration) -> float:
        """Most bits the view may add: the calibration's cap, and `FACE_CAP_BITS` for a face view
        whose null did not include other scams with the same face."""
        if kind == FACE_KIND and not calibration.null_same_face:
            return min(calibration.cap_bits, FACE_CAP_BITS)
        return calibration.cap_bits


def content_views(calibrations: Optional[dict[str, Calibration]], store: Optional[VectorStore]) -> Optional[ContentViews]:
    """A `ContentViews` switch, or None (off) when there is no calibration or no usable view."""
    if not calibrations:
        return None
    cv = ContentViews(dict(calibrations), store)
    return cv if cv.active() else None
