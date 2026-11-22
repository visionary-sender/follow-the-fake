"""Background rarity: how many distinct operators of a reference corpus carry a value.

`BackgroundCounts.from_dataset(corpus)` counts, per (layer, kind, normalised value), the distinct
*operators* carrying it. An operator is a ledger pre-group (incidents joined by a shared live
operator-private id, see `ledger._pre_groups`), so a large campaign's own ids count once, never
once per incident.

Smoothed background frequency (Dirichlet / add-α over the kind's vocabulary):

    u = (count + α) / (N_kind + α·V_kind)

with N_kind the operators carrying any value of the kind and V_kind its distinct values. Ids that
are unique by construction (UUIDs, 15–16-digit pixel ids, xid machine ids) get a type prior
u ≈ 1/N_kind instead: their observed frequency says only which level they name.

Values are stored as truncated, unkeyed SHA-256 digests of (layer, kind, value): the counts file
carries no raw indicator values (tracker ids, tokens in URLs) in plain text. Unkeyed hashes of
low-entropy values can be confirmed by hashing a guessed value, so the file stays a private
bucket artefact like the data it was counted from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ftf.contract.models import Dataset, Layer, linkable

ALPHA = 0.5
FORMAT = "ftf-background"
VERSION = 1

# Unique-by-construction id shapes: UUIDs, 15–16-digit pixel/account ids, xid ids.
UNIQUE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9]{15,16}|[0-9a-v]{20})$",
    re.IGNORECASE,
)
# Corpus sources without page indicators (CERT domain listings): left out, they would dilute the denominator.
SKIP_SOURCES = ("certpl",)
# corpus_kind.jsonl tags: which corpus incidents are rarity evidence, which are real outlet pages.
RARITY_KINDS = frozenset({"fetched_landing", "ad_record", "ad_library", "scanned_ad"})
MUST_NOT_LINK_KINDS = frozenset({"must_not_link", "must_not_link_blocked"})

# Share of a kind's values that must look unique by construction for the type prior to apply.
TYPE_PRIOR_SHARE = 0.8


def digest(layer: Layer | str, kind: str, norm: str) -> str:
    layer_name = layer.value if isinstance(layer, Layer) else layer
    return hashlib.sha256(f"{layer_name}\x1f{kind}\x1f{norm}".encode()).hexdigest()[:24]


def unique_by_construction(norm: str) -> bool:
    return bool(UNIQUE_ID.match(norm))


@dataclass
class KindStats:
    n_operators: int = 0  # operators carrying any value of the kind
    n_values: int = 0  # distinct values (V)
    unique_share: float = 0.0  # share of values shaped like unique-by-construction ids

    @property
    def type_prior(self) -> bool:
        return self.n_values > 0 and self.unique_share >= TYPE_PRIOR_SHARE


@dataclass
class BackgroundCounts:
    counts: dict[str, int] = field(default_factory=dict)  # digest(layer, kind, value) → operators
    kinds: dict[tuple[str, str], KindStats] = field(default_factory=dict)  # (layer, kind) → stats
    n_operators: int = 0
    n_incidents: int = 0
    sources: dict[str, int] = field(default_factory=dict)  # incident source → incidents (for the report)
    brand: set[str] = field(default_factory=set)  # digests of values seen on must-not-link real pages (brand skin)
    alpha: float = ALPHA

    @classmethod
    def from_dataset(
        cls, corpus: Dataset, alpha: float = ALPHA, must_not_link: Dataset | None = None,
        corpus_kinds: dict[str, str] | None = None, must_not_link_kinds: dict[str, str] | None = None,
    ) -> "BackgroundCounts":
        """Count distinct operators (pre-groups) per value of a background corpus dataset.

        Incidents of `SKIP_SOURCES` (no page indicators), incidents without any linkable indicator and,
        when `corpus_kinds` (incident id → corpus_kind) is given, incidents not tagged with a
        `RARITY_KINDS` kind (bare CERT domains, untagged) are left out: only they are operators. Several captures of one
        incident (a cloaked URL fetched twice) are one sighting: counts are per pre-group. Real outlet
        pages (`must_not_link`) never enter the counts or the denominator; their values only form a
        brand stop-list (a scam value also seen on a real page is brand skin, a hub)."""
        from ftf.linking.ledger import ScoringContext  # local: ledger imports this module

        kept = [
            i for i in corpus.incidents
            if not any(s in i.source or i.id.startswith(f"{s}:") for s in SKIP_SOURCES)
            and (corpus_kinds is None or corpus_kinds.get(i.id) in RARITY_KINDS)
            and any(linkable(x) and x.value is not None for x in i.indicators)
        ]
        ctx = ScoringContext.from_dataset(
            corpus.model_copy(update={"incidents": _detach(kept)}), chain_rule=False, brand=frozenset())
        brand: set[str] = set()
        if must_not_link is not None:
            if must_not_link_kinds is not None:
                must_not_link = must_not_link.model_copy(update={"incidents": _detach([
                    i for i in must_not_link.incidents if must_not_link_kinds.get(i.id) in MUST_NOT_LINK_KINDS])})
            brand = set(brand_digests(must_not_link))
        counts: dict[str, int] = {}
        kind_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
        kind_values: dict[tuple[str, str], int] = Counter()
        kind_unique: dict[tuple[str, str], int] = Counter()
        for (layer, kind, norm), incidents in ctx.index.items():
            if layer is Layer.collector:
                continue
            groups = {ctx.group_of[i] for i in incidents}
            counts[digest(layer, kind, norm)] = len(groups)
            key = (layer.value, kind)
            kind_groups[key] |= groups
            kind_values[key] += 1
            kind_unique[key] += unique_by_construction(norm)
        kinds = {
            key: KindStats(len(kind_groups[key]), kind_values[key], round(kind_unique[key] / kind_values[key], 3))
            for key in kind_values
        }
        return cls(
            counts=counts, kinds=kinds, n_operators=ctx.n_groups, n_incidents=ctx.n_incidents,
            sources=dict(Counter(inc.source for inc in kept)), alpha=alpha, brand=brand,
        )

    def count(self, layer: Layer, kind: str, norm: str) -> int:
        return self.counts.get(digest(layer, kind, norm), 0)

    def is_brand(self, layer: Layer, kind: str, norm: str) -> bool:
        return digest(layer, kind, norm) in self.brand

    def knows(self, layer: Layer, kind: str) -> bool:
        return (layer.value, kind) in self.kinds

    def stats(self, layer: Layer, kind: str) -> KindStats | None:
        return self.kinds.get((layer.value, kind))

    def u(self, layer: Layer, kind: str, norm: str) -> float | None:
        """Smoothed background frequency of a value, or None if the corpus never saw the kind."""
        st = self.stats(layer, kind)
        if st is None or st.n_operators == 0:
            return None
        if st.type_prior:
            return max(self.count(layer, kind, norm), 1) / st.n_operators
        return (self.count(layer, kind, norm) + self.alpha) / (st.n_operators + self.alpha * st.n_values)

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "version": VERSION,
            "alpha": self.alpha,
            "n_operators": self.n_operators,
            "n_incidents": self.n_incidents,
            "sources": self.sources,
            "kinds": [
                {"layer": layer, "kind": kind, "n_operators": st.n_operators, "n_values": st.n_values,
                 "unique_share": st.unique_share}
                for (layer, kind), st in sorted(self.kinds.items())
            ],
            "counts": dict(sorted(self.counts.items())),
            "brand": sorted(self.brand),
        }

    @classmethod
    def from_json(cls, doc: dict) -> "BackgroundCounts":
        if doc.get("format") != FORMAT or doc.get("version") != VERSION:
            raise ValueError(f"not an {FORMAT} v{VERSION} document")
        return cls(
            counts={str(k): int(v) for k, v in doc["counts"].items()},
            kinds={
                (k["layer"], k["kind"]): KindStats(int(k["n_operators"]), int(k["n_values"]), float(k["unique_share"]))
                for k in doc["kinds"]
            },
            n_operators=int(doc["n_operators"]), n_incidents=int(doc["n_incidents"]),
            sources={str(k): int(v) for k, v in doc.get("sources", {}).items()}, alpha=float(doc["alpha"]),
            brand={str(x) for x in doc.get("brand", [])},
        )


def read_corpus_kinds(dataset_path) -> dict[str, str] | None:
    """incident id → corpus_kind from `corpus_kind.jsonl` beside a corpus dataset, or None if absent."""
    import json
    from pathlib import Path

    path = Path(dataset_path).with_name("corpus_kind.jsonl")
    if not path.exists():
        return None
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["incident_id"])] = str(row.get("corpus_kind") or "")
    return out


def _detach(incidents: list) -> list:
    """Kept incidents whose captures point (`duplicate_of`) at a capture of a dropped incident become their own
    canonical capture, so a filtered corpus stays a consistent dataset."""
    kept = {c.id for i in incidents for c in i.captures}
    out = []
    for inc in incidents:
        caps = [c.model_copy(update={"duplicate_of": None}) if c.duplicate_of and c.duplicate_of not in kept else c
                for c in inc.captures]
        out.append(inc.model_copy(update={"captures": caps}))
    return out


# Brand stop-list: hashed values seen on real outlet pages (corpus batch 1, tagged
# must_not_link). Such a value is the impersonated outlet's skin (logo, fonts, favicon, the outlet's
# own analytics or CDN), so the ledger counts it under the ecosystem relation only. The file holds
# digests only and lives in the private bucket; the default scoring path loads it from
# $FTF_BRAND_STOPLIST, else DEFAULT_BRAND_STOPLIST. Missing file: a warning and no stop-list (the
# engine scores as before); FTF_BRAND_STOPLIST=off switches it off without a warning.
BRAND_FORMAT = "ftf-brand-stoplist"
BRAND_STOPLIST_ENV = "FTF_BRAND_STOPLIST"
DEFAULT_BRAND_STOPLIST = "/data/derived/linking/brand-stoplist/brand-stoplist.json"
_OFF = {"", "off", "none", "0"}


def brand_digests(must_not_link: Dataset) -> frozenset[str]:
    """Digests of every linkable (layer, kind, normalised value) on the real pages, collector layer excluded."""
    from ftf.linking.ledger import ScoringContext  # local: ledger imports this module

    real = ScoringContext.from_dataset(must_not_link, chain_rule=False, brand=frozenset())
    return frozenset(digest(*k) for k in real.index if k[0] is not Layer.collector)


def real_pages(corpus: Dataset, corpus_kinds: dict[str, str] | None) -> Dataset:
    """The corpus incidents tagged as real outlet pages (`MUST_NOT_LINK_KINDS`)."""
    kinds = corpus_kinds or {}
    return corpus.model_copy(update={"incidents": _detach([
        i for i in corpus.incidents if kinds.get(i.id) in MUST_NOT_LINK_KINDS])})


def brand_stoplist_doc(brand: frozenset[str] | set[str], n_pages: int) -> dict:
    return {"format": BRAND_FORMAT, "version": VERSION, "n_pages": n_pages, "brand": sorted(brand)}


def read_brand_stoplist(path: str | os.PathLike) -> frozenset[str]:
    """Digests from a brand stop-list file, or from the `brand` field of a background counts file."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") not in (BRAND_FORMAT, FORMAT):
        raise ValueError(f"{path}: not an {BRAND_FORMAT} or {FORMAT} document")
    return frozenset(str(x) for x in doc.get("brand", []))


def stoplist_path() -> str | None:
    """The configured stop-list path, None when switched off."""
    raw = os.environ.get(BRAND_STOPLIST_ENV)
    if raw is None:
        return DEFAULT_BRAND_STOPLIST
    return None if raw.strip().lower() in _OFF else raw


_LOADED: dict[tuple[str, float], frozenset[str]] = {}
_WARNED: set[str] = set()


def _load(path: str) -> frozenset[str]:
    """Read once per (path, mtime); a failed read is not cached, so a file written later is picked up."""
    try:
        key = (path, os.stat(path).st_mtime)
        if key not in _LOADED:
            _LOADED.clear()
            _LOADED[key] = read_brand_stoplist(path)
        return _LOADED[key]
    except (OSError, ValueError) as exc:
        if path in _WARNED:
            return frozenset()
        _WARNED.add(path)
        print(f"ftf: warning: brand stop-list not loaded ({exc}); brand-skin values are scored like any other "
              f"(set {BRAND_STOPLIST_ENV} to the file, or to 'off')", file=sys.stderr)
        return frozenset()


def default_brand() -> frozenset[str]:
    """The brand stop-list of the default scoring path (empty when off or missing, see module notes above)."""
    path = stoplist_path()
    return _load(path) if path else frozenset()
