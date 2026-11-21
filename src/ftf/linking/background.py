"""Background rarity: how many distinct operators of a reference corpus carry a value (07 §1.2, §2.2 item 2).

`BackgroundCounts.from_dataset(corpus)` counts, per (layer, kind, normalised value), the distinct
*operators* carrying it. An operator is a ledger pre-group (incidents joined by a shared live
operator-private id, see `ledger._pre_groups`), so a large campaign's own ids count once, never
once per incident.

Smoothed background frequency (Dirichlet / add-α over the kind's vocabulary):

    u = (count + α) / (N_kind + α·V_kind)

with N_kind the operators carrying any value of the kind and V_kind its distinct values. Ids that
are unique by construction (UUIDs, 15–16-digit pixel ids, xid machine ids) get a type prior
u ≈ 1/N_kind instead (07 §2.2 item 2): their observed frequency says only which level they name.

Values are stored as salted-free SHA-256 digests of (layer, kind, value): the counts file can be
shipped and logged without carrying raw indicator values (tracker ids, tokens in URLs).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ftf.contract.models import Dataset, Layer

ALPHA = 0.5
FORMAT = "ftf-background"
VERSION = 1

# Unique-by-construction id shapes (07 §2.2 item 2): UUIDs, 15–16-digit pixel/account ids, xid ids.
UNIQUE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9]{15,16}|[0-9a-v]{20})$",
    re.IGNORECASE,
)
# Corpus sources without page indicators (CERT domain listings): left out, they would dilute the denominator.
SKIP_SOURCES = ("certpl",)

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
        cls, corpus: Dataset, alpha: float = ALPHA, must_not_link: Dataset | None = None
    ) -> "BackgroundCounts":
        """Count distinct operators (pre-groups) per value of a background corpus dataset.

        Incidents of `SKIP_SOURCES` (no page indicators) are left out. Several captures of one
        incident (a cloaked URL fetched twice) are one sighting: counts are per pre-group. Real outlet
        pages (`must_not_link`) never enter the counts or the denominator; their values only form a
        brand stop-list (a scam value also seen on a real page is brand skin, a hub)."""
        from ftf.linking.ledger import ScoringContext  # local: ledger imports this module

        kept = [i for i in corpus.incidents if not any(s in i.source or i.id.startswith(f"{s}:") for s in SKIP_SOURCES)]
        ctx = ScoringContext.from_dataset(corpus.model_copy(update={"incidents": kept}), chain_rule=False)
        brand: set[str] = set()
        if must_not_link is not None:
            real = ScoringContext.from_dataset(must_not_link, chain_rule=False)
            brand = {digest(*k) for k in real.index if k[0] is not Layer.collector}
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
