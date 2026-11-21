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
  `assign` if the best score >= tau + log2 K and that pair qualifies as an edge (enough positive
  layers for the relation, or a live private id; see `grouping.MIN_LAYERS`); `new` if it is <= tau - 3; `review` otherwise. An item whose
  evidence capacity is below tau cannot reach the bar at all, so it goes to `review`, never `new`
  (a screenshot-only item is not proof of a new campaign).
* Evidence capacity = sum, over the layers the item can still show, of the most bits the weight
  table gives that layer. Which layers a modality can show is read from the collection itself
  (indicator provenance -> artefact type), not hard-coded.
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
from ftf.linking.grouping import MIN_LAYERS, SINGLE_VALUED_LAYERS, canonical_map, link_id, qualifies
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

    probe = _probe(incident, canon, stage)
    campaigns = [c for c in base.campaigns if c.relation is relation]
    tau = be.threshold(relation)
    k = len(campaigns)
    bar = tau + (math.log2(k) if k > 1 else 0.0)

    scored: list[tuple[Campaign, Optional[PairScore], dict[str, PairScore]]] = []
    for camp in campaigns:
        members = _members(camp, canon, stage) - set(probe)
        per_member: dict[str, PairScore] = {}
        for m in sorted(members):
            best_m: Optional[PairScore] = None
            for x in probe:
                ps = be.score(x, m, relation)
                if best_m is None or _rank(ps) > _rank(best_m):
                    best_m = ps
            if best_m is not None:
                per_member[m] = best_m
        top = max(per_member.values(), key=_rank, default=None)
        scored.append((camp, top, per_member))
    scored.sort(key=lambda t: (-(t[1].bits if t[1] else -math.inf), t[0].id))

    capacity, gains = evidence_capacity(combined, incident, probe, be.layer_caps(relation))

    candidates = [
        Candidate(campaign_id=camp.id, score_bits=round(ps.bits, 3), note=_candidate_note(ps))
        for camp, ps, _ in scored[:TOP_CANDIDATES]
        if ps is not None
    ]
    best_camp, best_ps = (scored[0][0], scored[0][1]) if scored and scored[0][1] is not None else (None, None)
    best = best_ps.bits if best_ps is not None else 0.0

    if best_ps is not None and best >= bar and qualifies(best_ps, tau, MIN_LAYERS[relation]):
        decision, campaign_id = Decision.assign, best_camp.id
    elif best <= tau - NEW_MARGIN and capacity >= tau:
        decision, campaign_id = Decision.new, None
    else:
        decision, campaign_id = Decision.review, None

    note = _note(relation, best, tau, bar, k, capacity, gains, best_ps)
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
    caps = list(dict.fromkeys(canon[c.id] for c in incident.captures))
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
) -> str:
    parts = [
        f"{relation.value}: best {best:.1f} bits vs assign bar {bar:.1f} (tau {tau:.1f} + log2 K, K={k}), new at <= {tau - NEW_MARGIN:.1f}",
        f"evidence capacity {capacity:.1f} bits",
    ]
    if best_ps is not None and best >= bar and not qualifies(best_ps, tau, MIN_LAYERS[relation]):
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
