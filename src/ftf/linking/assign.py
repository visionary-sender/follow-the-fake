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

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
from ftf.linking.types import METHOD, PairScore

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
) -> Assignment:
    """Assign `incident` to a campaign of `relation` in `collection`, or declare it new, or send it to review."""
    return link_incident(collection, incident, relation, backend=backend, base_dir=base_dir, incident_dir=incident_dir).assignment


def link_incident(
    base: Dataset,
    incident: Incident,
    relation: Relation = Relation.campaign,
    backend: Optional[BackendFactory] = None,
    base_dir: Optional[Path] = None,
    incident_dir: Optional[Path] = None,
) -> IncidentResult:
    """The assignment plus ledgers from the incident's captures to members of its top candidate campaigns.

    `base` is the output of `ftf link` / `link_dataset`. The links are valid inside `base` plus
    the incident. `base_dir` / `incident_dir` are where the base's and the incident's vector
    stores live (content views, HACK-24); while they are None, content views are skipped. They
    are accepted but not used yet.
    """
    if any(i.id == incident.id for i in base.incidents):
        raise ValueError(f"incident {incident.id} is already in the collection")
    combined = combine(base, [incident])
    be = (backend or Backend.from_dataset)(combined)
    canon = canonical_map(combined)
    stage = {c.id: c.stage for i in combined.incidents for c in i.captures}

    own = {c.id for c in incident.captures}
    probe = _probe(incident, canon, stage)
    # Exact duplicates of a collection capture: the incident is that capture again. Decoys do
    # not count when the incident has anything else.
    dup_src = [c for c in incident.captures if stage[canon[c.id]] is not Stage.decoy and c.stage is not Stage.decoy] or list(incident.captures)
    dup_of = sorted({canon[c.id] for c in dup_src if canon[c.id] not in own})
    blocks_on_conflict = relation in CANNOT_LINK_RELATIONS
    campaigns = [c for c in base.campaigns if c.relation is relation]
    tau = be.threshold(relation)
    k = len(campaigns)
    bar = tau + (math.log2(k) if k > 1 else 0.0)
    min_layers = MIN_LAYERS[relation]

    scored: list[tuple[Campaign, Optional[PairScore], dict[str, PairScore]]] = []
    conflicted: set[str] = set()  # campaigns with a tenant/owner conflict against the incident
    # probe capture -> campaign id -> best pair that clears the bar with a qualifying ledger
    by_probe: dict[str, dict[str, PairScore]] = defaultdict(dict)
    for camp in campaigns:
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
        top = max(per_member.values(), key=_rank, default=None)
        scored.append((camp, top, per_member))
    scored.sort(key=lambda t: (-(t[1].bits if t[1] else -math.inf), t[0].id))

    capacity, gains = evidence_capacity(combined, incident, probe or dup_of, be.layer_caps(relation))

    dup_camps = [camp for camp in campaigns if set(dup_of) & {canon.get(m, m) for m in camp.capture_ids}]
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

    reach = _lander_targets(incident, canon, stage, own, by_probe, campaigns)
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
    canon: dict[str, str],
    stage: dict[str, Stage],
    own: set[str],
    by_probe: dict[str, dict[str, PairScore]],
    campaigns: list[Campaign],
) -> dict[str, list[str]]:
    """Lander capture -> the campaigns it reaches on its own (best first), for incidents with several landers.

    A lander duplicating a collection capture reaches the one campaign holding that capture;
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
            holding = [c.id for c in campaigns if e in {canon.get(m, m) for m in c.capture_ids}]
            if len(holding) == 1:
                out[e] = holding
            continue
        ranked = sorted(by_probe.get(e, {}).items(), key=lambda kv: (-kv[1].bits, kv[0]))
        if ranked:
            out[e] = [cid for cid, _ in ranked]
    return out


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
    art_type = {a.id: a.type for i in ds.incidents for c in i.captures for a in c.artefacts}
    out: dict[Layer, set[ArtefactType]] = defaultdict(set)
    for ind in ds.indicators():
        if not linkable(ind):
            continue
        t = art_type.get(ind.provenance.artefact_id or "")
        if t is not None:
            out[ind.layer].add(t)
    return out


def evidence_capacity(
    ds: Dataset, incident: Incident, probe: list[str], caps: dict[Layer, float]
) -> tuple[float, list[tuple[ArtefactType, Optional[str], float, list[Layer]]]]:
    """Most bits the item could earn given its modalities, and what each absent modality could add.

    Returns (capacity, gains) where gains are (modality, missing reason or None, extra bits,
    layers) for every absent modality that would open a layer the item cannot show now.
    """
    probe_set = set(probe)
    captures = [c for c in incident.captures if c.id in probe_set or c.duplicate_of in probe_set]
    has = {t for c in captures for t in c.modalities}
    reasons: dict[ArtefactType, str] = {}
    for c in captures:
        for m in c.missing:
            reasons.setdefault(m.modality, m.reason)
    own_layers = {ind.layer for ind in incident.indicators if linkable(ind) and ind.capture_id in {c.id for c in captures}}
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
    conflicts = sorted({r.layer.value for r in ps.rows if r.status is EvidenceStatus.conflict and r.layer in SINGLE_VALUED_LAYERS})
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
