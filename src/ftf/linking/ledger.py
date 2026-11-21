"""Tiered evidence ledger (design D1): score two captures in bits, row by row.

Rules (07 §2.2, §3 D1):

- Unit = canonical capture. Captures with `duplicate_of` are collapsed onto their canonical
  capture before anything is counted, so duplicates inflate neither scores nor rarity.
- A shared value of a kind is worth `weight × relation scale − log2(k)`, k = distinct incidents
  whose (canonical) captures carry that value (a stand-in for distinct clusters), floored at 0.
  Below `hub_bits` it is `hub_subsumed` (0 bits).
- Within a layer only the best shared kind counts (layers are dependent inside); across layers
  the bits add. A conflict on a single-valued, campaign-stable field (platform / tenant / owner)
  adds `conflict_bits` once per layer (the worst one).
- Missing data is 0, never negative. A `missing` row appears only when one side lacks the layer.
- Collector indicators never contribute: a shared collector value is a `collector_excluded` row
  with 0 bits.
- Vector indicators (`vector_ref`) are ignored here (calibrated similarities come with HACK-24).

The sum of the row bits equals `PairScore.bits`: rows that lose the within-layer max carry 0 bits
and say so in their note.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ftf.contract.models import (
    Dataset,
    EvidenceRow,
    EvidenceStatus,
    Layer,
    Link,
    LinkLevel,
    Observed,
    Relation,
)
from ftf.linking.types import METHOD, PairScore
from ftf.linking.weights import HAND_TABLE, IGNORED_VALUES, WeightTable

# Typical campaign size m (in captures) per relation, for the prior log-odds −log2(n/m) (07 §2.4).
# Hand-set: an aio campaign in the sample spans ~3 captures; a tracking account or operator spans
# a few campaigns; a sold template and an ecosystem span many more.
TYPICAL_SIZE: dict[Relation, float] = {
    Relation.template: 8.0,
    Relation.tracking_account: 4.0,
    Relation.operator: 6.0,
    Relation.campaign: 3.0,
    Relation.ecosystem: 12.0,
}
THRESHOLD_MARGIN = 3.0  # bits above the prior log-odds (07 §2.4)
REVIEW_BAND = 3.0  # τ − 3 ≤ score < τ is "review" (D1 assignment rule)
DISPLAY_LEN = 40

Key = tuple[Layer, str]  # (layer, kind)


def normalise(value: str) -> str:
    """Comparison form of an indicator value: trimmed, inner whitespace collapsed, case-folded."""
    return " ".join(value.split()).casefold()


def _short(value: str, n: int = DISPLAY_LEN) -> str:
    return value if len(value) <= n else value[: n - 1] + "…"


@dataclass
class _Values:
    """One kind's values on one canonical capture: normalised value → indicator ids / display / live."""

    ids: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    display: dict[str, str] = field(default_factory=dict)
    live: set[str] = field(default_factory=set)


@dataclass
class ScoringContext:
    ds: Dataset
    weights: WeightTable
    canonical: dict[str, str]  # capture id → canonical capture id
    incident_of: dict[str, str]  # capture id → incident id
    indicator_incident: dict[str, str]  # indicator id → incident id
    values: dict[str, dict[Key, _Values]]  # canonical capture → (layer, kind) → values
    index: dict[tuple[Layer, str, str], set[str]]  # (layer, kind, normalised value) → incidents
    n: int  # canonical captures
    n_incidents: int  # incidents with at least one canonical capture

    @classmethod
    def from_dataset(cls, ds: Dataset, weights: WeightTable = HAND_TABLE) -> "ScoringContext":
        canonical: dict[str, str] = {}
        incident_of: dict[str, str] = {}
        for inc in ds.incidents:
            for cap in inc.captures:
                canonical[cap.id] = cap.duplicate_of or cap.id
                incident_of[cap.id] = inc.id
        canon_ids = {c for c in canonical.values()}
        values: dict[str, dict[Key, _Values]] = {c: {} for c in canon_ids}
        index: dict[tuple[Layer, str, str], set[str]] = defaultdict(set)
        for inc in ds.incidents:
            for ind in inc.indicators:
                if ind.value is None or normalise(ind.value) in IGNORED_VALUES:
                    continue
                cap = canonical[ind.capture_id]
                norm = normalise(ind.value)
                vals = values[cap].setdefault((ind.layer, ind.kind), _Values())
                vals.ids[norm].append(ind.id)
                vals.display.setdefault(norm, ind.value)
                if ind.observed is Observed.live:
                    vals.live.add(norm)
                # rarity counts the incident of the canonical capture, never the duplicate's
                index[(ind.layer, ind.kind, norm)].add(incident_of[cap])
        return cls(
            ds=ds,
            weights=weights,
            canonical=canonical,
            incident_of=incident_of,
            indicator_incident={ind.id: inc.id for inc in ds.incidents for ind in inc.indicators},
            values=values,
            index=dict(index),
            n=len(canon_ids),
            n_incidents=len({incident_of[c] for c in canon_ids}),
        )

    def resolve(self, capture_id: str) -> str:
        try:
            return self.canonical[capture_id]
        except KeyError:
            raise KeyError(f"unknown capture {capture_id}") from None


@dataclass
class _Candidate:
    row: EvidenceRow
    bits: float


def score_pair(ctx: ScoringContext, a: str, b: str, relation: Relation = Relation.campaign) -> PairScore:
    """Itemised ledger for captures `a` and `b` (resolved to canonical captures) under `relation`."""
    relation = Relation(relation)
    ca, cb = ctx.resolve(a), ctx.resolve(b)
    if ca == cb:
        raise ValueError(f"{a} and {b} are the same capture after collapsing duplicates ({ca})")
    w = ctx.weights
    va, vb = ctx.values[ca], ctx.values[cb]
    ends = {ctx.incident_of[ca], ctx.incident_of[cb]}
    layers_a = {layer for layer, _ in va}
    layers_b = {layer for layer, _ in vb}

    def ids_of(vals: _Values, norms) -> list[str]:
        # evidence may only cite indicators of the two linked incidents (contract invariant 5);
        # indicators merged in from duplicates elsewhere are dropped from the citation
        out = []
        for norm in sorted(norms):
            out += [i for i in vals.ids[norm] if ctx.indicator_incident[i] in ends]
        return out

    shared_rows: dict[Layer, list[_Candidate]] = defaultdict(list)
    conflict_rows: dict[Layer, list[_Candidate]] = defaultdict(list)
    other_rows: list[EvidenceRow] = []
    live_private = False

    for key in sorted(set(va) | set(vb), key=lambda k: (list(Layer).index(k[0]), k[1])):
        layer, kind = key
        xa, xb = va.get(key), vb.get(key)
        entry = w.weight(kind, layer)
        scale = w.scale(kind, layer, relation)
        if xa is None or xb is None:
            present = xa or xb
            side = "a" if xa is not None else "b"
            lacking = layers_b if xa is not None else layers_a
            if layer is not Layer.collector and layer not in lacking and entry.bits * scale >= w.hub_bits:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.missing,
                    value=_short(next(iter(present.display.values()))),
                    indicator_ids=ids_of(present, present.ids), note=f"only on {side}; other side lacks the {layer.value} layer",
                ))
            continue
        common = set(xa.ids) & set(xb.ids)
        if layer is Layer.collector:
            if common:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.collector_excluded,
                    value=_short(xa.display[min(common)]), seen_in=_seen(ctx, key, min(common)),
                    indicator_ids=ids_of(xa, common) + ids_of(xb, common),
                    note="describes the capturing analyst or tool, never links",
                ))
            continue
        if common:
            best_norm, best_bits, best_k, best_live = None, -1.0, 0, False
            for norm in sorted(common):
                k = len(ctx.index[(layer, kind, norm)])
                live = norm in xa.live and norm in xb.live
                prior = entry.bits * scale
                if entry.private and not live:
                    prior = max(prior - w.static_discount, 0.0)
                bits = max(prior - math.log2(max(k, 1)), 0.0)
                if bits > best_bits:
                    best_norm, best_bits, best_k, best_live = norm, bits, k, live
            ids = ids_of(xa, [best_norm]) + ids_of(xb, [best_norm])
            seen = _seen(ctx, key, best_norm)
            value = _short(xa.display[best_norm])
            if scale == 0:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.shared, value=value, seen_in=seen,
                    indicator_ids=ids, note=f"says nothing about relation {relation.value}",
                ))
                continue
            if best_bits < w.hub_bits:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.hub_subsumed, value=value, seen_in=seen,
                    indicator_ids=ids, note=f"{_fmt_weight(entry.bits, scale)} − log2({best_k}) < {w.hub_bits} bit",
                ))
                continue
            how = "live on both sides" if best_live else f"not live on both sides, −{w.static_discount:g} copy prior"
            note = f"{_fmt_weight(entry.bits, scale)} − log2({best_k}) rarity"
            if entry.private:
                note += f"; operator-private, {how}"
                live_private = live_private or best_live
            if len(common) > 1:
                note += f"; {len(common)} values shared, best kept"
            shared_rows[layer].append(_Candidate(EvidenceRow(
                layer=layer, kind=kind, status=EvidenceStatus.shared, value=value, bits=round(best_bits, 3),
                seen_in=seen, indicator_ids=ids, note=note,
            ), round(best_bits, 3)))
        elif entry.single_valued and scale > 0 and relation in w.conflict_relations:
            na, nb = sorted(xa.ids)[0], sorted(xb.ids)[0]
            conflict_rows[layer].append(_Candidate(EvidenceRow(
                layer=layer, kind=kind, status=EvidenceStatus.conflict,
                value=f"{_short(xa.display[na], 18)} vs {_short(xb.display[nb], 18)}", bits=w.conflict_bits,
                indicator_ids=ids_of(xa, xa.ids) + ids_of(xb, xb.ids),
                note="single-valued, campaign-stable field differs",
            ), w.conflict_bits))

    rows: list[EvidenceRow] = []
    total = 0.0
    layers_positive = 0
    for layer in Layer:
        layer_bits = 0.0
        cands = sorted(shared_rows.get(layer, []), key=lambda c: -c.bits)
        if cands:
            top = cands[0]
            rows.append(top.row)
            layer_bits += top.bits
            for c in cands[1:]:
                rows.append(c.row.model_copy(update={
                    "bits": 0.0, "note": f"{c.row.note}; within-layer max: subsumed by {top.row.kind}",
                }))
        confs = sorted(conflict_rows.get(layer, []), key=lambda c: c.bits)
        if confs:
            rows.append(confs[0].row)
            layer_bits += confs[0].bits
            for c in confs[1:]:
                rows.append(c.row.model_copy(update={"bits": 0.0, "note": f"{c.row.note}; counted once per layer"}))
        total += layer_bits
        if layer_bits > 0:
            layers_positive += 1
    rows += other_rows
    order = list(Layer)
    rows.sort(key=lambda r: (order.index(r.layer), r.kind))
    return PairScore(
        a=ca, b=cb, relation=relation, bits=round(total, 3), rows=rows,
        layers_positive=layers_positive, live_private=live_private,
    )


def threshold(ctx: ScoringContext, relation: Relation = Relation.campaign) -> float:
    """τ = log2(n/m) + 3 bits (07 §2.4); n = canonical captures, m = TYPICAL_SIZE[relation]."""
    m = TYPICAL_SIZE[Relation(relation)]
    return round(max(math.log2(max(ctx.n, 1) / m), 0.0) + THRESHOLD_MARGIN, 3)


def verdict(score: PairScore, ctx: ScoringContext) -> str:
    """`link` at or above τ, `review` within REVIEW_BAND below it, else `no link`."""
    tau = threshold(ctx, score.relation)
    if score.bits >= tau:
        return "link"
    return "review" if score.bits >= tau - REVIEW_BAND else "no link"


def level_of(score: PairScore) -> LinkLevel:
    if score.live_private or score.layers_positive >= 3:
        return LinkLevel.strict
    return LinkLevel.medium if score.layers_positive >= 2 else LinkLevel.loose


def to_link(score: PairScore, link_id: str, ctx: ScoringContext) -> Link:
    """A contract `Link` carrying the ledger. Raises ValueError if the ledger has no rows."""
    if not score.rows:
        raise ValueError(f"link {link_id}: empty ledger, nothing to explain")
    return Link(
        id=link_id, a=score.a, b=score.b, relation=score.relation, level=level_of(score),
        score_bits=score.bits, method=METHOD, evidence=list(score.rows),
    )


def _seen(ctx: ScoringContext, key: Key, norm: str) -> str:
    k = len(ctx.index.get((key[0], key[1], norm), ()))
    return f"{k} incident{'s' if k != 1 else ''} / {ctx.n_incidents}"


def _fmt_weight(bits: float, scale: float) -> str:
    return f"{bits:g}" if scale == 1 else f"{bits:g}×{scale:g}"

