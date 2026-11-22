"""Open-set assignment of a new incident to the campaigns of one relation: assign, new or review.

Method (07 §2.4, D1/D2 "Assignment"):

* The new incident's probe captures are its canonical non-decoy captures (its decoys only if it
  has nothing else). The scoring context is built over the collection plus the incident, so
  rarity counts include it.
* Score against a campaign = the best pairwise ledger between a probe capture and a campaign
  member (D1: max over members; a variant only needs to match *some* member). Pairs with an
  informative ledger rank above pairs that share nothing, so a conflict on a lander is not
  hidden behind a 0-bit ad-to-ad pair.
* With tau = `threshold()` (it scales with the collection size) and K campaigns of the relation:
  `assign` to the best campaign whose score is >= tau + log2 K and whose best pair qualifies as an edge (enough positive
  layers for the relation, or a live private id; see `grouping.MIN_LAYERS`); `new` if it is <= tau - 3; `review` otherwise. An item whose
  evidence capacity is below tau cannot reach the bar at all, so it goes to `review`, never `new`
  (a screenshot-only item is not proof of a new campaign).
* Evidence capacity = sum, over the layers the item can still show, of the most bits the weight
  table gives that layer. Which layers a modality can show is read from the collection itself
  (indicator provenance -> artefact type), not hard-coded.
* An incident whose (non-decoy) capture is an exact duplicate (`duplicate_of`) of a collection
  capture is assigned to the campaign holding that capture; if that capture is in no campaign
  or in several, it goes to review.
* The decision is made per incident. An incident with several landers (end points, see
  `grouping.incident_chain`) may hold unrelated sites (HACK-57): each lander is matched on its
  own (the campaigns it clears the bar against with a qualifying ledger, or the campaign
  holding the capture it duplicates). If no campaign is reached by every pointing lander, the
  incident goes to review, with a note naming each lander's best campaign. An assignment to a
  campaign outside that common set (won through an ad capture) also goes to review.
* Under operator / campaign a tenant or owner conflict between any probe capture and a member
  of the chosen campaign (cannot-link) sends the incident to review instead of assigning it.
* The collection is not re-clustered.
"""

from __future__ import annotations

import bisect
import math
from collections import ChainMap, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from ftf.contract.models import (
    ArtefactType,
    Assignment,
    Campaign,
    Candidate,
    Dataset,
    Decision,
    EvidenceStatus,
    Incident,
    Layer,
    Link,
    Relation,
    Stage,
    linkable,
)
from ftf.linking.backend import Backend
from ftf.linking.grouping import CANNOT_LINK_RELATIONS, MIN_LAYERS, SINGLE_VALUED_LAYERS, canonical_map, has_conflict, incident_chain, link_id, qualifies
from ftf.linking.index import CandidateIndex, cached_index, dataset_digest, modalities_of
from ftf.linking.ledger import ScoringContext
from ftf.linking.types import METHOD, PairScore
from ftf.linking.weights import WeightTable

NEW_MARGIN = 3.0  # bits below tau at which an item is declared new
TOP_CANDIDATES = 5
LINKED_CANDIDATES = 3  # campaigns whose members get ledgers in `link_incident`

BackendFactory = Callable[[Dataset], Backend]


@dataclass
class IncidentResult:
    assignment: Assignment
    links: list[Link] = field(default_factory=list)


def assign(
    collection: Dataset,
    incident: Incident,
    relation: Relation = Relation.campaign,
    backend: Optional[BackendFactory] = None,
    base_dir: Optional[Path] = None,
    incident_dir: Optional[Path] = None,
    index: Optional[CandidateIndex] = None,
) -> Assignment:
    """Assign `incident` to a campaign of `relation` in `collection`, or declare it new, or send it to review."""
    return link_incident(
        collection, incident, relation, backend=backend, base_dir=base_dir, incident_dir=incident_dir, index=index
    ).assignment


def link_incident(
    base: Dataset,
    incident: Incident,
    relation: Relation = Relation.campaign,
    backend: Optional[BackendFactory] = None,
    base_dir: Optional[Path] = None,
    incident_dir: Optional[Path] = None,
    index: Optional["CandidateIndex"] = None,
    weights: Optional[WeightTable] = None,
) -> IncidentResult:
    """The assignment plus ledgers from the incident's captures to members of its top candidate campaigns.

    `base` is the output of `ftf link` / `link_dataset`. The links are valid inside `base` plus
    the incident. `base_dir` / `incident_dir` are where the base's and the incident's vector
    stores live (content views, HACK-24); while they are None, content views are skipped. They
    are accepted but not used yet.

    With a `CandidateIndex` of `base` (given, or built earlier for this very object and cached),
    only the campaigns reachable through the incident's informative values are scored and the
    scoring context is extended instead of rebuilt; the result is identical to the linear scan.
    `weights` replaces the weight table (sensitivity analysis; needs the hand-table ledger). An index
    scores with the background counts and chain rule setting it was built with; after `index.add()`
    it stands for its base plus the added incidents (`base` must still be the base it was built for).
    """
    if index is None and backend is None:
        # The cached index was built with the hand-table ledger; a custom scorer may match on
        # other kinds, so it always takes the linear scan unless an index is passed explicitly.
        index = cached_index(base)
    if index is not None and index._base is not base and (not index.digest or dataset_digest(base) != index.digest):
        raise ValueError("the index belongs to another base dataset; build or load it for this one")
    if index is not None and backend is None and weights is not None and index.drops_bits_of(weights):
        # `weights` gives bits to a kind the index kept no postings for: candidate search would miss
        # it, so score every campaign of the collection the index stands for, with its options.
        base, backend = index.linear_view()
        index = None
    if index is not None:
        return _decide(_indexed_setup(index, incident, relation, backend, weights), incident, relation)
    if any(i.id == incident.id for i in base.incidents):
        raise ValueError(f"incident {incident.id} is already in the collection")
    combined = combine(base, [incident])
    be = (backend or Backend.from_dataset)(combined)
    if weights is not None:
        be = _reweighted(be, weights)
    setup = _Setup(
        be=be,
        canon=canonical_map(combined),
        stage={c.id: c.stage for i in combined.incidents for c in i.captures},
        modalities=layer_modalities(combined),
        campaigns=[c for c in base.campaigns if c.relation is relation],
    )
    return _decide(setup, incident, relation)


@dataclass
class _Setup:
    be: Backend
    canon: Mapping[str, str]
    stage: Mapping[str, Stage]
    modalities: dict[Layer, set[ArtefactType]]
    campaigns: Sequence[Any]  # `Campaign` or `CampaignRef` (both have .id and .capture_ids)
    # Indexed mode: the campaigns to score first, the campaigns of a capture, and sorted ids.
    reachable: Optional[Callable[[list[str]], set[str]]] = None
    campaigns_of: Optional[Callable[[str], Sequence[str]]] = None
    # Real ledger only: False if the two captures may get a conflict row under the relation.
    conflict_free: Optional[Callable[[str, str], bool]] = None


def _reweighted(be: Backend, weights: WeightTable) -> Backend:
    if not isinstance(be.context, ScoringContext):
        raise ValueError("weights= needs the hand-table ledger backend")
    return Backend.from_context(replace(be.context, weights=weights))


def _indexed_setup(
    index: "CandidateIndex", incident: Incident, relation: Relation, backend: Optional[BackendFactory], weights: Optional[WeightTable]
) -> _Setup:
    ctx = index.extend(incident)
    if weights is not None:
        ctx = replace(ctx, weights=weights)
    if backend is not None:
        if weights is not None:
            raise ValueError("weights= needs the hand-table ledger backend")
        # A custom scorer (tests) needs its own view of base + incident; candidate search still
        # comes from the index, and every shared value proposes campaigns.
        if index._base is None:
            raise ValueError("a custom backend with an index needs the index built from the base dataset")
        be = backend(combine(index._base, [*index._added, incident]))
        informative = None
    else:
        be = Backend.from_context(ctx)
        hub = ctx.weights.hub_bits

        def informative(key) -> bool:
            # The most bits this value can earn in base + incident (live prior, combined rarity).
            prior = ctx.weights.weight(key[1], key[0]).bits * ctx.weights.scale(key[1], key[0], relation)
            return prior - ctx.rarity(key, ctx.groups(key)) >= hub

    keys = index.probe_keys(incident)
    modalities = {layer: set(mods) for layer, mods in index.modalities.items()}
    for layer, mods in modalities_of([incident]).items():
        modalities.setdefault(layer, set()).update(mods)
    of = index.campaign_of.get(relation, {})
    return _Setup(
        be=be,
        canon=ctx.canonical,
        stage=ChainMap({c.id: c.stage for c in incident.captures}, index.stage),
        modalities=modalities,
        campaigns=index.campaigns.get(relation, []),
        reachable=lambda extra: index.reachable(keys, relation, informative=informative or (lambda key: True), captures=extra),
        conflict_free=None if backend is not None else _conflict_free(ctx, relation),
        campaigns_of=lambda cap: of.get(cap, ()),
    )


def _decide(setup: _Setup, incident: Incident, relation: Relation) -> IncidentResult:
    be, canon, stage = setup.be, setup.canon, setup.stage
    own = {c.id for c in incident.captures}
    probe = _probe(incident, canon, stage)
    # Exact duplicates of a collection capture: the incident is that capture again. Decoys do
    # not count when the incident has anything else.
    dup_src = [c for c in incident.captures if stage[canon[c.id]] is not Stage.decoy and c.stage is not Stage.decoy] or list(incident.captures)
    dup_of = sorted({canon[c.id] for c in dup_src if canon[c.id] not in own})
    blocks_on_conflict = relation in CANNOT_LINK_RELATIONS
    campaigns = setup.campaigns
    tau = be.threshold(relation)
    k = len(campaigns)
    bar = tau + (math.log2(k) if k > 1 else 0.0)
    min_layers = MIN_LAYERS[relation]

    conflicted: set[str] = set()  # campaigns with a tenant/owner conflict against the incident
    # probe capture -> campaign id -> best pair that clears the bar with a qualifying ledger.
    # Filled by `score`; independent of scoring order, and the same on the indexed path: a pair
    # that clears the bar shares informative values, so its campaign is always reachable.
    by_probe: dict[str, dict[str, PairScore]] = defaultdict(dict)

    def score(camp: Any) -> tuple[Any, Optional[PairScore], dict[str, PairScore]]:
        members = _members(camp, canon, stage)
        per_member: dict[str, PairScore] = {}
        for m in sorted(members):
            best_m: Optional[PairScore] = None
            for x in probe:
                ps = be.score(x, m, relation)
                if blocks_on_conflict and has_conflict(ps):
                    conflicted.add(camp.id)
                prev = by_probe[x].get(camp.id)
                if ps.bits >= bar and qualifies(ps, tau, min_layers) and (prev is None or ps.bits > prev.bits):
                    by_probe[x][camp.id] = ps
                if best_m is None or _rank(ps) > _rank(best_m):
                    best_m = ps
            if best_m is not None:
                per_member[m] = best_m
        return camp, max(per_member.values(), key=_rank, default=None), per_member

    if setup.reachable is None:
        scored = [score(camp) for camp in campaigns]
        dup_camps = [camp for camp in campaigns if set(dup_of) & {canon.get(m, m) for m in camp.capture_ids}]
    else:
        assert setup.campaigns_of is not None
        dup_ids = {cid for d in dup_of for cid in setup.campaigns_of(d)}
        quick = None
        if setup.conflict_free is not None and probe:
            cf = setup.conflict_free

            def quick(camp: Any):
                # An unreachable campaign shares no informative value, so with no conflict row its
                # score is exactly 0 and the linear scan keeps the first pair (first probe, first member).
                members = sorted(_members(camp, canon, stage))
                if all(cf(x, m) for m in members for x in probe):
                    return camp, be.score(probe[0], members[0], relation), {}
                return None

        # Every collection capture the incident duplicates (decoys too) takes in the incident's
        # values, so its campaigns must be scored even when no shared value reaches them.
        merged_into = sorted({canon[c.id] for c in incident.captures if canon[c.id] not in own})
        scored = _select(campaigns, setup.reachable(merged_into) | dup_ids, score, quick)
        dup_camps = [camp for camp in campaigns if camp.id in dup_ids]
    scored.sort(key=_order)

    capacity, gains = evidence_capacity(None, incident, probe or dup_of, be.layer_caps(relation), lm=setup.modalities)

    dup_camp = dup_camps[0] if len(dup_camps) == 1 else None
    candidates = [
        Candidate(campaign_id=camp.id, score_bits=round(ps.bits, 3), note=_candidate_note(ps))
        for camp, ps, _ in scored[:TOP_CANDIDATES]
        if ps is not None
    ]
    if dup_camp is not None and all(c.campaign_id != dup_camp.id for c in candidates):
        top_dup = next(ps for camp, ps, _ in scored if camp.id == dup_camp.id)
        candidates.append(
            Candidate(
                campaign_id=dup_camp.id,
                score_bits=round(top_dup.bits, 3) if top_dup is not None else 0.0,
                note="exact duplicate of a member capture",
            )
        )
    best_camp, best_ps = (scored[0][0], scored[0][1]) if scored and scored[0][1] is not None else (None, None)
    best = best_ps.bits if best_ps is not None else 0.0
    # The assign target is the best campaign that clears the bar with a qualifying ledger,
    # not necessarily the top-scoring one (that one may rest on a single layer).
    target = next(
        (camp for camp, ps, _ in scored if ps is not None and ps.bits >= bar and qualifies(ps, tau, min_layers)),
        None,
    )

    if setup.campaigns_of is not None:
        holding = setup.campaigns_of
    else:

        def holding(cap: str) -> list[str]:
            return [c.id for c in campaigns if cap in {canon.get(m, m) for m in c.capture_ids}]

    reach = _lander_targets(incident, canon, stage, own, by_probe, holding)
    pointed = {e: cids[0] for e, cids in reach.items()}
    common = set.intersection(*(set(cids) for cids in reach.values())) if reach else set()
    disagree = len(reach) > 1 and not common

    if disagree:
        # Several landers of one incident point to different campaigns: the folder may hold
        # unrelated sites, so a person decides.
        decision, campaign_id = Decision.review, None
    elif dup_of and (dup_camp is None or dup_camp.id in conflicted):
        # A duplicate of a capture in no campaign, in several, or one whose campaign conflicts
        # with the incident's other captures: a person decides.
        decision, campaign_id = Decision.review, None
    elif dup_camp is not None:
        decision, campaign_id = Decision.assign, dup_camp.id
    elif target is not None and target.id in conflicted:
        decision, campaign_id = Decision.review, None
    elif target is not None:
        decision, campaign_id = Decision.assign, target.id
        if all(c.campaign_id != target.id for c in candidates):
            ps = next(p for camp, p, _ in scored if camp.id == target.id)
            candidates.append(Candidate(campaign_id=target.id, score_bits=round(ps.bits, 3), note=_candidate_note(ps)))
    elif best <= tau - NEW_MARGIN and capacity >= tau:
        decision, campaign_id = Decision.new, None
    else:
        decision, campaign_id = Decision.review, None

    off_lander = decision is Decision.assign and bool(reach) and campaign_id not in common
    if off_lander:
        decision, campaign_id = Decision.review, None
    for cid in sorted(set(pointed.values()) - {c.campaign_id for c in candidates}):
        ps = next((p for camp, p, _ in scored if camp.id == cid), None)
        candidates.append(Candidate(campaign_id=cid, score_bits=round(ps.bits, 3) if ps is not None else 0.0, note="a lander of the incident points here"))

    note = _note(relation, best, tau, bar, k, capacity, gains, best_ps, min_layers)
    if dup_of:
        where = f"campaign {dup_camp.id}" if dup_camp is not None else f"{len(dup_camps)} campaigns"
        note = f"exact duplicate of {', '.join(dup_of)} ({where}); " + note
    if decision is Decision.review and conflicted & {c.id for c in [target, dup_camp] if c is not None}:
        note = "tenant/owner conflict with the best campaign; " + note
    if off_lander:
        named = "; ".join(f"{x} -> {cid}" for x, cid in sorted(pointed.items()))
        note = f"best match is not the campaign the landers point to ({named}); " + note
    if disagree:
        named = "; ".join(f"{x} -> {cid}" for x, cid in sorted(pointed.items()))
        note = f"landers disagree ({named}); " + note
    assignment = Assignment(
        incident_id=incident.id,
        candidates=candidates,
        decision=decision,
        campaign_id=campaign_id,
        evidence_capacity_bits=round(max(capacity, 0.0), 3),
        method=METHOD,
        note=note,
    )

    links: list[Link] = []
    seen: set[str] = set()
    for camp, ps, per_member in scored[:LINKED_CANDIDATES]:
        if ps is None or ps.bits <= 0:
            continue
        for m, mps in sorted(per_member.items()):
            if mps.bits <= 0:
                continue
            lid = link_id(relation, mps.a, mps.b, prefix=f"link:{incident.id}")
            if lid in seen:
                continue
            link = be.link(mps, lid)
            if link is not None:
                seen.add(lid)
                links.append(link)
    return IncidentResult(assignment=assignment, links=links)


def _lander_targets(
    incident: Incident,
    canon: Mapping[str, str],
    stage: Mapping[str, Stage],
    own: set[str],
    by_probe: Mapping[str, dict[str, PairScore]],
    holding: Callable[[str], Sequence[str]],
) -> dict[str, list[str]]:
    """Lander capture -> the campaigns it reaches on its own (best first), for incidents with several landers.

    A lander duplicating a collection capture reaches the one campaign holding that capture
    (`holding(capture)` lists the campaigns of a canonical collection capture);
    any other lander every campaign it clears the bar against with a qualifying ledger
    (`by_probe`). Landers reaching nothing are left out. Empty when the incident has at most
    one lander.
    """
    ends, _ = incident_chain(incident, canon, stage)
    if len(ends) < 2:
        return {}
    out: dict[str, list[str]] = {}
    for e in ends:
        if e not in own:
            held = list(holding(e))
            if len(held) == 1:
                out[e] = held
            continue
        ranked = sorted(by_probe.get(e, {}).items(), key=lambda kv: (-kv[1].bits, kv[0]))
        if ranked:
            out[e] = [cid for cid, _ in ranked]
    return out


def _order(t: tuple[Any, Optional[PairScore], dict]) -> tuple[float, str]:
    """The linear scan's ranking: bits descending (no score last), then campaign id."""
    return (-(t[1].bits if t[1] is not None else -math.inf), t[0].id)


def _select(
    campaigns: Sequence[Any],
    reachable: set[str],
    score: Callable[[Any], tuple[Any, Optional[PairScore], dict]],
    quick: Optional[Callable[[Any], Optional[tuple[Any, Optional[PairScore], dict]]]] = None,
) -> list[tuple[Any, Optional[PairScore], dict]]:
    """Score the reachable campaigns, then just enough others to rank the top as the linear scan does.

    `campaigns` must be sorted by id. Unreachable campaigns share no informative value with the
    incident, so they score 0 bits (or less, on a conflict). Only when fewer than
    `TOP_CANDIDATES` campaigns score above 0 does the candidate list reach into them: they are
    then walked in id order (the scan's tie order) until the zero-bit places are settled, or
    all of them are scored. Everything that decides assign / new / review is reachable.
    `quick(camp)` returns a campaign's exact entry without scoring all its pairs when it can
    prove the score is 0 (no conflict possible), else None.
    """
    by_id = {c.id: c for c in campaigns}
    out = [score(by_id[cid]) for cid in sorted(reachable) if cid in by_id]
    positive = sum(1 for _, ps, _ in out if ps is not None and ps.bits > 0)
    need = TOP_CANDIDATES - positive
    if need <= 0 or all(ps is None for _, ps, _ in out) and out:
        return out  # top settled, or no probe capture (nothing gets a score)
    zero_r = sorted(c.id for c, ps, _ in out if ps is not None and ps.bits == 0)
    zero_walked = 0
    for camp in campaigns:
        if camp.id in reachable:
            continue
        t = (quick(camp) if quick is not None else None) or score(camp)
        out.append(t)
        if t[1] is None:
            return out  # no probe capture: nothing gets a score
        zero_walked += t[1].bits == 0
        if bisect.bisect_right(zero_r, camp.id) + zero_walked >= need:
            break
    return out


def _conflict_free(ctx: ScoringContext, relation: Relation) -> Callable[[str, str], bool]:
    """True if `score_pair(x, m)` can have no conflict row: the ledger's conflict test, without the ledger.

    A conflict row needs a single-valued kind (scale > 0, relation in `conflict_relations`)
    on both captures with no value in common.
    """
    w = ctx.weights
    if relation not in w.conflict_relations:
        return lambda x, m: True
    single = {}

    def risky(key) -> bool:
        if key not in single:
            layer, kind = key
            single[key] = layer is not Layer.collector and w.weight(kind, layer).single_valued and w.scale(kind, layer, relation) > 0
        return single[key]

    def ok(x: str, m: str) -> bool:
        va, vb = ctx.values[x], ctx.values[m]
        if len(vb) < len(va):
            va, vb = vb, va
        for key, xa in va.items():
            xb = vb.get(key)
            if xb is not None and risky(key) and not (xa.ids.keys() & xb.ids.keys()):
                return False
        return True

    return ok


def _rank(ps: PairScore) -> tuple[bool, float]:
    """Order pairs by bits, but an informative ledger (any non-zero row) beats an empty one.

    Otherwise an ad screenshot that shares nothing (0 bits) would hide a lander's conflict.
    """
    return (any(r.bits != 0 for r in ps.rows), ps.bits)


def combine(base: Dataset, incidents: list[Incident]) -> Dataset:
    data = base.model_dump(mode="json")
    data["incidents"] = data["incidents"] + [i.model_dump(mode="json") for i in incidents]
    return Dataset.model_validate(data)


def _probe(incident: Incident, canon: dict[str, str], stage: dict[str, Stage]) -> list[str]:
    """The incident's own canonical captures, non-decoy first (decoys only if nothing else).

    Captures that duplicate a collection capture are left out: they are handled as exact
    duplicates, and scoring them would put two collection captures in the incident's links.
    """
    own = {c.id for c in incident.captures}
    caps = list(dict.fromkeys(canon[c.id] for c in incident.captures if canon[c.id] in own))
    non_decoy = [c for c in caps if stage[c] is not Stage.decoy]
    return non_decoy or caps


def _members(camp: Campaign, canon: dict[str, str], stage: dict[str, Stage]) -> set[str]:
    members = {canon.get(m, m) for m in camp.capture_ids}
    non_decoy = {m for m in members if stage.get(m) is not Stage.decoy}
    return non_decoy or members


def layer_modalities(ds: Dataset) -> dict[Layer, set[ArtefactType]]:
    """Which artefact types each layer's indicators were extracted from, across the collection."""
    return modalities_of(ds.incidents)


def evidence_capacity(
    ds: Optional[Dataset],
    incident: Incident,
    probe: list[str],
    caps: dict[Layer, float],
    lm: Optional[dict[Layer, set[ArtefactType]]] = None,
) -> tuple[float, list[tuple[ArtefactType, Optional[str], float, list[Layer]]]]:
    """Most bits the item could earn given its modalities, and what each absent modality could add.

    Returns (capacity, gains) where gains are (modality, missing reason or None, extra bits,
    layers) for every absent modality that would open a layer the item cannot show now.
    `lm` (layer -> modalities of the collection plus the incident) replaces the scan of `ds`.
    """
    probe_set = set(probe)
    captures = [c for c in incident.captures if c.id in probe_set or c.duplicate_of in probe_set]
    has = {t for c in captures for t in c.modalities}
    reasons: dict[ArtefactType, str] = {}
    for c in captures:
        for m in c.missing:
            reasons.setdefault(m.modality, m.reason)
    own_layers = {ind.layer for ind in incident.indicators if linkable(ind) and ind.capture_id in {c.id for c in captures}}
    if lm is None:
        if ds is None:
            raise ValueError("evidence_capacity needs the collection or its layer modalities")
        lm = layer_modalities(ds)
    reachable = own_layers | {layer for layer, mods in lm.items() if mods & has}
    capacity = sum(max(caps.get(layer, 0.0), 0.0) for layer in reachable)

    all_mods = {t for mods in lm.values() for t in mods} | set(reasons)
    gains = []
    for mod in sorted(all_mods - has, key=lambda m: m.value):
        opened = sorted((layer for layer, mods in lm.items() if mod in mods and layer not in reachable), key=lambda l: l.value)
        extra = sum(max(caps.get(layer, 0.0), 0.0) for layer in opened)
        if extra > 0:
            gains.append((mod, reasons.get(mod), extra, opened))
    gains.sort(key=lambda g: (-g[2], g[0].value))
    return capacity, gains


def _strong_layers(ps: PairScore, n: int = 3) -> str:
    agg: dict[str, float] = defaultdict(float)
    for r in ps.rows:
        if r.status is EvidenceStatus.shared and r.bits > 0:
            agg[r.layer.value] += r.bits
    top = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{k.replace('_', ' ')} +{v:.1f}" for k, v in top)


def _candidate_note(ps: PairScore) -> str:
    parts = []
    strong = _strong_layers(ps)
    parts.append(f"strongest: {strong}" if strong else "no shared layer")
    # name the kind too: an aio campaign id filed in the owner layer is no owner conflict (not a cannot-link)
    conflicts = sorted({f"{r.layer.value} {r.kind}" for r in ps.rows if r.status is EvidenceStatus.conflict and r.layer in SINGLE_VALUED_LAYERS})
    if conflicts:
        parts.append("conflict: " + ", ".join(conflicts))
    parts.append(f"best pair {ps.a} ~ {ps.b}")
    return "; ".join(parts)


def _note(
    relation: Relation,
    best: float,
    tau: float,
    bar: float,
    k: int,
    capacity: float,
    gains: list[tuple[ArtefactType, Optional[str], float, list[Layer]]],
    best_ps: Optional[PairScore],
    min_layers: int,
) -> str:
    parts = [
        f"{relation.value}: best {best:.1f} bits vs assign bar {bar:.1f} (tau {tau:.1f} + log2 K, K={k}), new at <= {tau - NEW_MARGIN:.1f}",
        f"evidence capacity {capacity:.1f} bits",
    ]
    if best_ps is not None and best >= bar and not qualifies(best_ps, tau, min_layers):
        parts.append("best match rests on one layer only")
    if capacity < tau:
        parts.append("capacity below tau: the inputs present cannot prove a new campaign")
    if gains:
        for mod, reason, extra, layers in gains[:3]:
            what = ", ".join(l.value.replace("_", " ") for l in layers)
            why = f" ({reason})" if reason else ""
            parts.append(f"{mod.value} missing{why}: up to +{extra:.1f} bits from {what}")
    else:
        parts.append("no missing input could add evidence under this relation")
    return "; ".join(parts)
