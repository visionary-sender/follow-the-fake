"""Tiered evidence ledger (design D1): score two captures in bits, row by row.

Rules (07 §2.2, §3 D1):

- Unit = canonical capture. Captures with `duplicate_of` are collapsed onto their canonical
  capture before anything is counted, so duplicates inflate neither scores nor rarity.
- A shared value of a kind is worth `weight × relation scale − log2(k)`, floored at 0, where k is
  the number of distinct *pre-groups* among the incidents carrying the value (07 §2.2 item 2:
  rarity over clusters, not incidents, so a big campaign's own ids are not punished). Pre-groups
  are the connected components of incidents that share a live operator-private id, joined from
  the finest level available (owner, then tracker instance / live pixel, then tenant; see
  `_pre_groups`); an incident without one is its own group. A coarse id spanning several owners
  is therefore counted at that coarser level (07 §2.2 item 5). With background counts
  (`background.py`) the rarity term is log2[(c + α)/(1 + α)], c the larger of the corpus operators
  carrying the value and the sample's pre-groups: 0 for a value of one operator, as above.
- Chain rule (`hierarchy.py`): values whose discovered level (platform ⊃ tenant ⊃ owner ⊃ aio
  campaign ⊃ landing) is already counted by that level's shared id, or by a stronger value of the
  same level, become `hub_subsumed` before the within-layer max. One term per level.
- A conflict on a single-valued tenant/owner id is a cannot-link for the campaign and operator
  relations (07 §3): `verdict` never says `link` for such a pair. The bits stay honest (−4 on the
  conflict row); HACK-22 reads the conflict rows for its cannot-link constraints.
  Below `hub_bits` it is `hub_subsumed` (0 bits).
- Within a layer only the best shared kind counts (layers are dependent inside); across layers
  the bits add. A conflict on a single-valued, campaign-stable field (platform / tenant / owner)
  adds `conflict_bits` once per layer (the worst one).
- Campaign gate (HACK-53): under the campaign relation the D1 gate (≥2 positive layers or a live
  operator-private id) also needs one positive shared value of campaign-level evidence (`weights.CAMPAIGN_EVIDENCE_KINDS`:
  the ad and its creative, ad account campaign or ad set, landing or own domain, aio campaign, offer id; or a
  value the id hierarchy places at campaign or landing level; for a kind without a weight entry in the creative,
  distribution or landing layer, its own `Indicator.level` campaign or landing), read before the chain rule and the
  within-layer max. A value the hierarchy places at operator level or above never counts,
  and a differing aio campaign id without a shared aio landing closes the gate. Without it `layers_positive` is capped at 1 and `live_private` is false, so
  every consumer of the gate (verdict, grouping, assignment, link level) sees at most a review: one
  operator's pixel, CAPI gateway and kit span several of its campaigns (07 §2.2, §2.3). The bits
  are untouched.
- Missing data is 0, never negative. A `missing` row appears only when one side lacks the layer.
- Collector indicators never contribute: a shared collector value is a `collector_excluded` row
  with 0 bits.
- Content views (HACK-24, off by default): with `ScoringContext.content` set (fitted
  calibrations plus a vector store, `similarity.ContentViews`), each calibrated view (text and
  image embeddings, perceptual hashes, MinHash) adds one capped shared row in its indicator's own
  layer, scaled by that layer's relation scale (halved again under operator), so the within-layer
  max applies against the exact-match kinds of that layer. A face view is capped at
  `similarity.FACE_CAP_BITS` unless its null held other scams with the same face. Content rows add
  bits only: they never make a layer positive, never open the campaign gate and are never a live
  private id, so content similarity alone can send an item to review but never to a link or an
  assignment. Off, the ledger is exactly what it was.

The sum of the row bits equals `PairScore.bits`: rows that lose the within-layer max carry 0 bits
and say so in their note.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from ftf.contract.models import (
    Dataset,
    EvidenceRow,
    EvidenceStatus,
    Layer,
    Level,
    Link,
    LinkLevel,
    Observed,
    Relation,
    linkable,
)
from ftf.linking.background import BackgroundCounts
from ftf.linking.hierarchy import ANCHOR_KINDS, ChainRow, Hierarchy, apply_chain_rule
from ftf.linking.similarity import OPERATOR_FACTOR, ContentViews, view_evidence
from ftf.linking.types import METHOD, PairScore
from ftf.linking.weights import HAND_TABLE, IGNORED_VALUES, WeightTable, is_campaign_evidence

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


# Free-text layers compare case-insensitively; ids, hashes and URL paths stay case-sensitive.
CASE_FOLDED_LAYERS = frozenset({Layer.creative, Layer.distribution_account, Layer.offer})


def normalise(value: str, layer: Layer | None = None) -> str:
    """Comparison form of an indicator value: trimmed, inner whitespace collapsed; case-folded only
    for free-text layers (`CASE_FOLDED_LAYERS`), never for ids, where case is significant."""
    out = " ".join(value.split())
    return out.casefold() if layer in CASE_FOLDED_LAYERS else out


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
    layers: dict[str, set[Layer]]  # canonical capture → layers it has any indicator in (vectors too)
    index: dict[tuple[Layer, str, str], set[str]]  # (layer, kind, normalised value) → incidents
    group_of: dict[str, str]  # incident id → pre-group id (see module docstring)
    n_groups: int
    n: int  # canonical captures
    n_incidents: int  # incidents with at least one canonical capture
    background: BackgroundCounts | None = None  # corpus operator counts for the rarity term
    hierarchy: Hierarchy | None = None  # discovered id levels for the chain rule (None: off)
    kind_level: dict[Key, Level] = field(default_factory=dict)  # (layer, kind) → its usual Indicator.level
    content: ContentViews | None = None  # calibrated content views (HACK-24); None: off
    content_inds: dict[str, dict[str, list]] = field(default_factory=dict)  # canonical capture → view kind → indicators

    @classmethod
    def from_dataset(
        cls, ds: Dataset, weights: WeightTable = HAND_TABLE, background: BackgroundCounts | None = None,
        chain_rule: bool = True, content: ContentViews | None = None,
    ) -> "ScoringContext":
        """Index `ds` for scoring. `background` switches the rarity term to corpus operator counts;
        `chain_rule` discovers the id hierarchy of `ds` and applies one term per level; `content`
        switches on the calibrated content views (see `with_content`)."""
        canonical: dict[str, str] = {}
        incident_of: dict[str, str] = {}
        for inc in ds.incidents:
            for cap in inc.captures:
                canonical[cap.id] = cap.duplicate_of or cap.id
                incident_of[cap.id] = inc.id
        canon_ids = {c for c in canonical.values()}
        values: dict[str, dict[Key, _Values]] = {c: {} for c in canon_ids}
        layers: dict[str, set[Layer]] = {c: set() for c in canon_ids}
        index: dict[tuple[Layer, str, str], set[str]] = defaultdict(set)
        live_private: dict[tuple[Layer, str, str], set[str]] = defaultdict(set)
        owners: dict[str, set[str]] = defaultdict(set)  # incident → owner-level ids (live or static)
        levels: dict[Key, Counter] = defaultdict(Counter)
        for inc in ds.incidents:
            for ind in inc.indicators:
                cap = canonical[ind.capture_id]
                layers[cap].add(ind.layer)
                if ind.value is None:
                    continue
                norm = normalise(ind.value, ind.layer)
                if not norm or norm.casefold() in IGNORED_VALUES:
                    continue
                vals = values[cap].setdefault((ind.layer, ind.kind), _Values())
                vals.ids[norm].append(ind.id)
                vals.display.setdefault(norm, ind.value)
                if ind.observed is Observed.live:
                    vals.live.add(norm)
                # rarity counts the incident of the canonical capture, never the duplicate's
                index[(ind.layer, ind.kind, norm)].add(incident_of[cap])
                levels[(ind.layer, ind.kind)][ind.level] += 1
                if is_owner_id(ind):
                    owners[incident_of[cap]].add(f"{ind.kind}={norm}")
                if ind.observed is Observed.live and weights.weight(ind.kind, ind.layer).private:
                    live_private[(ind.layer, ind.kind, norm)].add(incident_of[cap])
        group_of = _pre_groups({incident_of[c] for c in canon_ids}, live_private, owners)
        ctx = cls(
            ds=ds,
            weights=weights,
            canonical=canonical,
            incident_of=incident_of,
            indicator_incident={ind.id: inc.id for inc in ds.incidents for ind in inc.indicators},
            values=values,
            layers=layers,
            index=dict(index),
            group_of=group_of,
            n_groups=len(set(group_of.values())),
            n=len(canon_ids),
            n_incidents=len({incident_of[c] for c in canon_ids}),
            background=background,
            kind_level={key: usual_level(c) for key, c in levels.items()},
        )
        if chain_rule:
            ctx.hierarchy = ctx.build_hierarchy()
        return with_content(ctx, content)

    def build_hierarchy(self, anchors=ANCHOR_KINDS) -> Hierarchy:
        caps = {
            cap: [(layer, kind, norm) for (layer, kind), v in vals.items() if layer is not Layer.collector for norm in v.ids]
            for cap, vals in self.values.items()
        }
        return Hierarchy.from_captures(caps, anchors)

    def rarity(self, key: tuple[Layer, str, str], k: int) -> float:
        """Bits taken off a shared value for being common: log2(k) over the sample's pre-groups, or with
        background counts log2[(c + α)/(1 + α)], c = max(corpus operators, sample pre-groups) (0 for one
        operator). The max, not the sum: a corpus that contains the scored sample must not count it twice."""
        if self.background is not None and self.background.is_brand(*key):
            return math.inf  # brand skin: also on a real outlet page, a hub
        if self.background is None or not self.background.knows(key[0], key[1]):
            return math.log2(max(k, 1))
        a = self.background.alpha
        c = max(self.background.count(*key), k, 1)
        return math.log2((c + a) / (1 + a))

    def groups(self, key: tuple[Layer, str, str]) -> int:
        """Distinct pre-groups among the incidents carrying this (layer, kind, value)."""
        return len({self.group_of[i] for i in self.index.get(key, ())})

    def resolve(self, capture_id: str) -> str:
        try:
            return self.canonical[capture_id]
        except KeyError:
            raise KeyError(f"unknown capture {capture_id}") from None


CONTENT_MARK = "content view, capped at"  # in the note of every content-view row


def is_content_row(row: EvidenceRow) -> bool:
    """True for a row added by a calibrated content view (HACK-24)."""
    return row.status is EvidenceStatus.shared and CONTENT_MARK in (row.note or "")


def with_content(ctx: ScoringContext, content: ContentViews | None) -> ScoringContext:
    """`ctx` with the content views switched on (a new context), or `ctx` itself when `content` is
    None or has no usable view (off)."""
    if content is None or not content.active():
        return ctx if ctx.content is None else replace(ctx, content=None, content_inds={})
    kinds = {view.kind for view, _ in content.active()}
    inds: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for inc in ctx.ds.incidents:
        for ind in inc.indicators:
            if ind.kind in kinds and linkable(ind):
                inds[ctx.canonical[ind.capture_id]][ind.kind].append(ind)
    return replace(ctx, content=content, content_inds={c: dict(v) for c, v in inds.items()})


def _content_rows(ctx: ScoringContext, ca: str, cb: str, ends: set[str], relation: Relation) -> list[EvidenceRow]:
    """One capped shared row per calibrated content view the two captures are similar on."""
    assert ctx.content is not None
    w = ctx.weights
    out: list[EvidenceRow] = []
    by_a, by_b = ctx.content_inds.get(ca, {}), ctx.content_inds.get(cb, {})
    for view, cal in ctx.content.active():
        # evidence may only cite indicators of the two linked incidents (contract invariant 5)
        a = [i for i in by_a.get(view.kind, ()) if ctx.indicator_incident.get(i.id) in ends]
        b = [i for i in by_b.get(view.kind, ()) if ctx.indicator_incident.get(i.id) in ends]
        if not a or not b:
            continue
        row = view_evidence(view, a, b, ctx.content.store, cal)
        if row is None or row.status is not EvidenceStatus.shared:
            continue
        scale = w.scale(view.kind, row.layer, relation) * (OPERATOR_FACTOR if relation is Relation.operator else 1.0)
        cap = ctx.content.cap(view.kind, cal)
        bits = round(min(row.bits, cap) * scale, 3)
        if bits < w.hub_bits:
            continue
        note = f"{row.note}; {CONTENT_MARK} {cap:g} bits"
        if scale != 1:
            note += f", ×{scale:g} for {relation.value}"
        if cap < cal.cap_bits:
            note += "; a face match alone says only: same celebrity"
        out.append(row.model_copy(update={"bits": bits, "note": note}))
    return out


@dataclass
class _Candidate:
    row: EvidenceRow
    bits: float
    content: bool = False  # a calibrated content view: adds bits, never makes the layer positive


def score_pair(ctx: ScoringContext, a: str, b: str, relation: Relation = Relation.campaign) -> PairScore:
    """Itemised ledger for captures `a` and `b` (resolved to canonical captures) under `relation`."""
    relation = Relation(relation)
    ca, cb = ctx.resolve(a), ctx.resolve(b)
    if ca == cb:
        raise ValueError(f"{a} and {b} are the same capture after collapsing duplicates ({ca})")
    w = ctx.weights
    va, vb = ctx.values[ca], ctx.values[cb]
    ends = {ctx.incident_of[ca], ctx.incident_of[cb]}
    layers_a, layers_b = ctx.layers[ca], ctx.layers[cb]

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
    shared_cands: list[ChainRow] = []

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
            best_norm, best_bits, best_k, best_live, best_rar = None, -1.0, 0, False, 0.0
            for norm in sorted(common):
                k = ctx.groups((layer, kind, norm))
                live = norm in xa.live and norm in xb.live
                prior = entry.bits * scale
                if entry.private and not live:
                    prior = max(prior - w.static_discount, 0.0)
                rar = ctx.rarity((layer, kind, norm), k)
                bits = max(prior - rar, 0.0)
                if bits > best_bits:
                    best_norm, best_bits, best_k, best_live, best_rar = norm, bits, k, live, rar
            ids = ids_of(xa, [best_norm]) + ids_of(xb, [best_norm])
            seen = _seen(ctx, key, best_norm)
            value = _short(xa.display[best_norm])
            if math.isinf(best_rar):
                rarity_txt = "brand skin (on a real outlet page)"
            elif ctx.background is not None and ctx.background.knows(layer, kind):
                rarity_txt = f"{best_rar:.2f} (corpus operators)"
            else:
                rarity_txt = f"log2({best_k})"
            if scale == 0:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.shared, value=value, seen_in=seen,
                    indicator_ids=ids, note=f"says nothing about relation {relation.value}",
                ))
                continue
            if best_bits < w.hub_bits:
                other_rows.append(EvidenceRow(
                    layer=layer, kind=kind, status=EvidenceStatus.hub_subsumed, value=value, seen_in=seen,
                    indicator_ids=ids, note=f"{_fmt_weight(entry.bits, scale)} − {rarity_txt} < {w.hub_bits} bit",
                ))
                continue
            how = "live on both sides" if best_live else f"not live on both sides, −{w.static_discount:g} copy prior"
            note = f"{_fmt_weight(entry.bits, scale)} − {rarity_txt} rarity"
            if entry.private:
                note += f"; operator-private, {how}"
            if len(common) > 1:
                note += f"; {len(common)} values shared, best kept"
            shared_cands.append(ChainRow(EvidenceRow(
                layer=layer, kind=kind, status=EvidenceStatus.shared, value=value, bits=round(best_bits, 3),
                seen_in=seen, indicator_ids=ids, note=note,
            ), round(best_bits, 3), (layer, kind, best_norm), live_private=entry.private and best_live, private=entry.private))
        elif entry.single_valued and scale > 0 and relation in w.conflict_relations:
            na, nb = sorted(xa.ids)[0], sorted(xb.ids)[0]
            conflict_rows[layer].append(_Candidate(EvidenceRow(
                layer=layer, kind=kind, status=EvidenceStatus.conflict,
                value=f"{_short(xa.display[na], 18)} vs {_short(xb.display[nb], 18)}", bits=w.conflict_bits,
                indicator_ids=ids_of(xa, xa.ids) + ids_of(xb, xb.ids),
                note="single-valued, campaign-stable field differs",
            ), w.conflict_bits))

    # campaign gate (HACK-53), read before the chain rule and the within-layer max: those only decide which
    # row carries a level's bits, so a shared aio campaign id that loses its slot to a kit value constant on
    # that campaign still proves the campaign
    def proves_campaign(c: ChainRow) -> bool:
        level = ctx.hierarchy.level(c.key) if ctx.hierarchy is not None else None
        if level in (Level.campaign, Level.landing):
            return True
        if level is not None:
            return False  # the value spans several campaigns of its owner (or more), whatever its kind
        if is_campaign_evidence(c.key[0], c.key[1]):
            return True
        # a kind without a weight entry in a campaign-ish layer: its own Indicator.level decides
        return (c.key[1] not in w.kinds and c.key[0] in _CAMPAIGN_FALLBACK_LAYERS
                and ctx.kind_level.get(c.key[:2]) in (Level.campaign, Level.landing))

    campaign_evidence = relation is not Relation.campaign or (
        any(c.bits > 0 and proves_campaign(c) for c in shared_cands)
        # two different aio campaign ids and no shared landing: two campaigns, whatever else they share
        and not (
            any(r.row.kind == "aio_campaign" for rs in conflict_rows.values() for r in rs)
            and not any(c.bits > 0 and c.key[1] == "aio_landing" for c in shared_cands)
        )
    )

    if ctx.hierarchy is not None and relation in CHAIN_RELATIONS:
        shared_cands, subsumed = apply_chain_rule(shared_cands, ctx.hierarchy, _platform_cap(w, relation))
        other_rows += subsumed
        shared_cands, explained = _explain_away(shared_cands, conflict_rows, ctx.hierarchy)
        other_rows += explained
    for c in shared_cands:
        shared_rows[c.key[0]].append(_Candidate(c.row, c.bits))
    if ctx.content is not None:
        # after the identifier rows of each layer, so on equal bits an identifier keeps the slot
        for row in _content_rows(ctx, ca, cb, ends, relation):
            shared_rows[row.layer].append(_Candidate(row, row.bits, content=True))
    # a live operator-private id counts for the strict gate only if it still carries bits after the chain rule
    live_private = any(c.live_private and c.bits > 0 for c in shared_cands)
    # the terms of one id chain (tenant ⊃ owner ⊃ aio campaign ⊃ landing) are one positive layer for the D1
    # gate and `level_of`: implied ancestor levels are not independent layers of evidence
    chain_kinds = {(c.key[0], c.key[1]) for c in shared_cands if c.chain}
    chain_positive = False

    slots = _chain_slots(ctx) if relation in CHAIN_RELATIONS else {}
    rows: list[EvidenceRow] = []
    total = 0.0
    layers_positive = 0
    for layer in Layer:
        layer_bits = 0.0
        # within a layer the best kind counts; in a layer holding chain ids (tenant, owner, aio campaign,
        # landing) the best kind per chain level counts, so a shared owner and a shared aio campaign are two
        # terms, while an alias of the campaign id or a page shape next to the landing id is not (`_slot`)
        tops: dict[Level | None, _Candidate] = {}
        id_tops: dict[Level | None, _Candidate] = {}  # the same without content rows: decides positivity
        # a chain level's term owns its slot: an alias in the same slot (the campaign name next to the campaign
        # id) must not escape the conditioning by outscoring it
        for c in sorted(shared_rows.get(layer, []), key=lambda c: ((layer, c.row.kind) not in chain_kinds, -c.bits)):
            slot = _slot(ctx, slots, layer, c.row.kind)
            if not c.content:
                id_tops.setdefault(slot, c)
            top = tops.get(slot)
            if top is None:
                tops[slot] = c
                rows.append(c.row)
                layer_bits += c.bits
            else:
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
        # positive layers count identifier evidence only; without content rows id_tops is tops
        id_bits = sum(c.bits for c in id_tops.values()) + (confs[0].bits if confs else 0.0)
        if id_bits > 0:
            own = [c for c in id_tops.values() if c.bits > 0 and (layer, c.row.kind) not in chain_kinds]
            if own or not any(c.bits > 0 for c in id_tops.values()):
                layers_positive += 1
            else:
                chain_positive = True
    layers_positive += chain_positive
    if not campaign_evidence:
        # operator-level evidence alone (accounts, kit, hosting) never makes a campaign link: review at most
        layers_positive = min(layers_positive, 1)
        live_private = False
    rows += other_rows
    order = list(Layer)
    rows.sort(key=lambda r: (order.index(r.layer), r.kind))
    return PairScore(
        a=ca, b=cb, relation=relation, bits=round(total, 3), rows=rows,
        layers_positive=layers_positive, live_private=live_private,
    )


def threshold(ctx: ScoringContext, relation: Relation = Relation.campaign) -> float:
    """τ = log2(n/m) + 3 bits (07 §2.4); n = canonical captures, m = TYPICAL_SIZE[relation].

    Deliberate deviation: log2(n/m) is floored at 0, so τ never drops below the 3-bit margin on a
    collection smaller than one typical group (a negative prior log-odds term would make a single
    weak row link)."""
    m = TYPICAL_SIZE[Relation(relation)]
    return round(max(math.log2(max(ctx.n, 1) / m), 0.0) + THRESHOLD_MARGIN, 3)


def verdict(score: PairScore, ctx: ScoringContext) -> str:
    """D1 edge rule: `link` if bits ≥ τ and (≥2 positive layers or a live operator-private id);
    a tenant/owner conflict under campaign/operator is a cannot-link (at most `review`);
    `review` if it clears τ without the gate, or lies within REVIEW_BAND below τ (and above 0
    bits); else `no link`."""
    tau = threshold(ctx, score.relation)
    if score.bits >= tau:
        cannot_link = score.relation in ctx.weights.conflict_relations and _private_conflict(score)
        gated = score.layers_positive >= 2 or score.live_private
        return "link" if gated and not cannot_link else "review"
    return "review" if score.bits > 0 and score.bits >= tau - REVIEW_BAND else "no link"


def has_private_conflict(score: PairScore) -> bool:
    """A tenant or owner conflict: the two captures belong to different operators (cannot-link). A differing
    chain id finer than the owner (an aio campaign id, which Ingestion files in the owner layer) is a
    conflict row for the campaign relation, never a cannot-link: one owner runs several campaigns."""
    return any(
        r.status is EvidenceStatus.conflict and r.layer in (Layer.tenant, Layer.owner)
        and ANCHOR_KINDS.get(r.kind, Level.operator) in (Level.tenant, Level.operator)
        for r in score.rows
    )


_private_conflict = has_private_conflict


def level_of(score: PairScore) -> LinkLevel:
    """strict: a live operator-private id or ≥3 positive layers; medium: ≥2 positive layers;
    loose: anything else. A tenant/owner conflict caps the level at medium (cannot-link pair)."""
    if _private_conflict(score):
        return LinkLevel.medium if score.layers_positive >= 2 else LinkLevel.loose
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
    full = (key[0], key[1], norm)
    k, i = ctx.groups(full), len(ctx.index.get(full, ()))
    return f"{k} group{'s' if k != 1 else ''} ({i} incident{'s' if i != 1 else ''}) / {ctx.n_groups}"


def is_owner_id(ind) -> bool:
    """An owner-level id, which feeds the pre-groups: owner layer, operator level. An aio campaign id
    filed in the owner layer names a campaign, not an owner."""
    return ind.layer is Layer.owner and (ind.level is Level.operator or ANCHOR_KINDS.get(ind.kind) is Level.operator)


def usual_level(counts: Counter) -> Level:
    """A kind's usual Indicator.level (`ScoringContext.kind_level`): the most frequent, ties by name."""
    return max(counts, key=lambda lv: (counts[lv], lv.value))


def _pre_groups(
    incidents: set[str], live_private: dict[tuple[Layer, str, str], set[str]], owners: dict[str, set[str]]
) -> dict[str, str]:
    """Connected components (union-find) of incidents sharing a live operator-private id, finest first.

    Every component carries the owner-level ids (live or static) of its incidents.
    1. Live owner ids join their carriers.
    2. Tracker instance / tracking account ids (live pixel) and fallback private kinds join two
       components unless both hold owner ids and none is shared (two different owners stay apart).
    3. Tenant ids join two components only if neither holds an owner id, or both hold a shared
       one: a tenant can be a reseller hosting several owners (07 §2.2 item 5).
    """
    parent = {i: i for i in incidents}
    own = {i: frozenset(owners.get(i, ())) for i in incidents}  # per root

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def tier(layer: Layer) -> int:
        return 0 if layer is Layer.owner else 2 if layer is Layer.tenant else 1

    def allowed(t: int, oa: frozenset, ob: frozenset) -> bool:
        if t == 0 or oa & ob:
            return True
        if t == 1:
            return not (oa and ob)
        return not oa and not ob

    for t in (0, 1, 2):
        for key in sorted(k for k in live_private if tier(k[0]) == t):
            first, *rest = sorted(live_private[key])
            for other in rest:
                ra, rb = find(first), find(other)
                if ra == rb or not allowed(t, own[ra], own[rb]):
                    continue
                root, child = min(ra, rb), max(ra, rb)
                parent[child] = root
                own[root] = own[ra] | own[rb]
    return {i: find(i) for i in incidents}

def _fmt_weight(bits: float, scale: float) -> str:
    return f"{bits:g}" if scale == 1 else f"{bits:g}×{scale:g}"


def _platform_cap(w: WeightTable, relation: Relation) -> float:
    """Most a shared platform may contribute: the strongest platform-layer kind of the table (07 §2.5: one
    term for all platform constants)."""
    bits = [e.bits * w.scale(k, Layer.platform, relation) for k, e in w.kinds.items() if e.layer is Layer.platform]
    bits.append(w.layer_defaults.get(Layer.platform, 0.0) * w.relation_scale.get(Layer.platform, {}).get(relation, 0.0))
    return max(bits)


# The chain (platform ⊃ tenant ⊃ owner ⊃ aio campaign ⊃ landing) is the operator's own hierarchy: the chain
# rule and per-level slots apply to these relations only. Template and tracking-account evidence (a kit or a
# pixel shared across tenants) is exactly what those relations look for.
CHAIN_RELATIONS = frozenset({Relation.operator, Relation.campaign, Relation.ecosystem})
# Layers in which a kind without a weight entry may count as campaign evidence by its own Indicator.level
# (HACK-53 campaign gate); elsewhere an unknown kind never opens the gate.
_CAMPAIGN_FALLBACK_LAYERS = frozenset({Layer.creative, Layer.distribution_account, Layer.landing})
# The chain level a layer's own anchor names; non-anchor kinds of a chain layer share that slot.
_LAYER_LEVEL = {Layer.tenant: Level.tenant, Layer.owner: Level.operator, Layer.landing: Level.landing}


def _chain_slots(ctx: ScoringContext) -> dict[Layer, set[Level]]:
    """Per layer, the chain levels of the anchor kinds observed in it (empty when the chain rule is off)."""
    out: dict[Layer, set[Level]] = defaultdict(set)
    if ctx.hierarchy is None:
        return out
    for layer, kind in ctx.kind_level:
        level = ctx.hierarchy.anchors.get(kind)
        if level is not None:
            out[layer].add(level)
    return out


def _slot(ctx: ScoringContext, slots: dict[Layer, set[Level]], layer: Layer, kind: str) -> Level | None:
    """Which term of its layer a kind competes for: anchors by their chain level; another kind joins the
    anchor slot of its own Indicator.level if the layer has one (an alias of the campaign id), else the
    slot of the layer's own anchor (a page shape next to the landing id)."""
    present = slots.get(layer)
    if not present:
        return None
    level = ctx.hierarchy.anchors.get(kind)
    if level is not None:
        return level
    level = ctx.kind_level.get((layer, kind))
    if level in present:
        return level
    own = _LAYER_LEVEL.get(layer)
    return own if own in present else min(present, key=lambda lv: list(Level).index(lv))


def _explain_away(cands: list, conflict_rows: dict, h) -> tuple[list, list[EvidenceRow]]:
    """A differing chain id (e.g. another aio campaign) under shared coarser ids (the same owner and tenant):
    "same owner, other campaign" predicts the shared ancestors as well as "same campaign" does, so the
    ancestors' chain terms carry no evidence for the relation and become `hub_subsumed`."""
    from ftf.linking.hierarchy import CHAIN, LEVEL_NAMES

    levels = [h.anchors[c.row.kind] for cs in conflict_rows.values() for c in cs if c.row.kind in h.anchors]
    if not levels:
        return cands, []
    finest = min(levels, key=CHAIN.index)
    kept, out = [], []
    for c in cands:
        level = h.level(c.key)
        if c.chain and level in CHAIN and CHAIN.index(level) > CHAIN.index(finest) and c.bits > 0:
            out.append(c.row.model_copy(update={
                "status": EvidenceStatus.hub_subsumed, "bits": 0.0,
                "note": f"{c.row.note}; chain rule: explained by the differing {LEVEL_NAMES[finest]} under it",
            }))
        else:
            kept.append(c)
    return kept, out
