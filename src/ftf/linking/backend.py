"""The scorer as grouping and assignment see it: one small object, so tests can plug in a stub.

`Backend.from_dataset` wires the hand-table ledger (HACK-21); tests build a `Backend` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from ftf.contract.models import Dataset, Layer, Link, LinkLevel, Relation
from ftf.linking.types import METHOD, PairScore, PairScorer


@dataclass
class Backend:
    dataset: Dataset
    score: PairScorer
    threshold: Callable[[Relation], float]
    # Most bits each layer can add under a relation (from the weight table); drives evidence capacity.
    layer_caps: Callable[[Relation], dict[Layer, float]]
    to_link: Optional[Callable[[PairScore, str], Link]] = None
    context: Any = None

    def link(self, ps: PairScore, link_id: str) -> Optional[Link]:
        """A contract link carrying the ledger, or None when the ledger has no rows to explain."""
        if not ps.rows:
            return None
        if self.to_link is not None:
            return self.to_link(ps, link_id)
        return default_link(ps, link_id)

    @classmethod
    def from_context(cls, ctx: Any) -> "Backend":
        from ftf.linking import ledger

        return cls(
            dataset=ctx.ds,
            score=lambda a, b, relation: ledger.score_pair(ctx, a, b, relation),
            threshold=lambda relation: ledger.threshold(ctx, relation),
            layer_caps=lambda relation: layer_caps(ctx.weights, relation),
            to_link=lambda ps, link_id: ledger.to_link(ps, link_id, ctx),
            context=ctx,
        )

    @classmethod
    def from_dataset(cls, ds: Dataset) -> "Backend":
        from ftf.linking.ledger import ScoringContext

        return cls.from_context(ScoringContext.from_dataset(ds))


def layer_caps(weights: Any, relation: Relation) -> dict[Layer, float]:
    """Most bits one shared value of each layer can add under `relation`, read from a `WeightTable`.

    Per layer: the largest `bits * scale` over the table's kinds in that layer and the layer
    default (the fallback for unknown kinds). Rarity only lowers a weight, so this is an upper bound.
    """
    caps: dict[Layer, float] = {}
    for layer in Layer:
        if layer is Layer.collector:
            continue
        best = weights.layer_defaults.get(layer, 0.0) * weights.relation_scale.get(layer, {}).get(relation, 0.0)
        for kind, entry in weights.kinds.items():
            if entry.layer is layer:
                best = max(best, entry.bits * weights.scale(kind, layer, relation))
        if best > 0:
            caps[layer] = best
    return caps


def link_level(ps: PairScore) -> LinkLevel:
    """Tiers posted on HACK-21: strict = live private id or >= 3 positive layers, medium = 2, loose otherwise."""
    if ps.live_private or ps.layers_positive >= 3:
        return LinkLevel.strict
    if ps.layers_positive >= 2:
        return LinkLevel.medium
    return LinkLevel.loose


def default_link(ps: PairScore, link_id: str) -> Optional[Link]:
    if not ps.rows:
        return None
    return Link(
        id=link_id,
        a=ps.a,
        b=ps.b,
        relation=ps.relation,
        level=link_level(ps),
        score_bits=round(ps.bits, 3),
        method=METHOD,
        evidence=list(ps.rows),
    )
