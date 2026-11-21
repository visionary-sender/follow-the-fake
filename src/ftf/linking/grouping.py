"""One partition of the captures per relation, with cannot-link on conflicting single-valued ids.

Method (07 §1.8, D2 "Linking / clustering"):

* Candidate pairs come from an inverted index over linkable indicator values: two captures are
  scored only if they share at least one value, or (operator / campaign relations) both carry a
  single-valued id (platform, tenant, owner) so the ledger can report a conflict.
* An edge qualifies when bits >= tau and (>= 2 positive layers or a live operator-private id).
  The signed weight of a pair is `bits - tau` for qualifying edges, `min(bits - tau, 0)`
  otherwise, and `-tau` for a pair that shares nothing (0 bits).
* Clustering is greedy average linkage on those signed weights (GAEC-style correlation
  clustering): repeatedly merge the two clusters with the highest mean cross weight while that
  mean is positive, they are joined by at least one qualifying edge, and no cross pair is
  cannot-linked. Unlike connected components, one false private-id match between two groups
  is outvoted by all the other cross pairs, so it does not chain them.
* Non-decoy captures of one incident are one ad's redirect chain: they start in one unit
  (must-link), unless two of them conflict. Between units the best capture pair counts
  (D1: max over members). A decoy capture is what the cloaker shows non-targets, so it is
  never must-linked to its incident: it stands alone and joins a cluster only on its own
  evidence, and cannot pull its incident anywhere.
* Every cluster with at least one non-decoy capture becomes a `Campaign`, singletons included,
  so each relation's campaigns are a full partition of the non-decoy captures and assignment
  can match a new item to a collection item that has no group yet. Decoy-only singletons are
  left out. Exact duplicates (`duplicate_of`) follow their canonical capture.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from ftf.contract.models import (
    Campaign,
    Dataset,
    EvidenceStatus,
    Layer,
    Link,
    Relation,
    Stage,
    linkable,
)
from ftf.linking.backend import Backend
from ftf.linking.types import PairScore

ALL_RELATIONS: tuple[Relation, ...] = tuple(Relation)
SINGLE_VALUED_LAYERS = frozenset({Layer.platform, Layer.tenant, Layer.owner})
CANNOT_LINK_RELATIONS = frozenset({Relation.operator, Relation.campaign})
# A value carried by more captures than this is a hub: it carries ~0 bits, so it never proposes pairs.
HUB_POSTINGS = 500


def qualifies(ps: PairScore, tau: float) -> bool:
    return ps.bits >= tau and (ps.layers_positive >= 2 or ps.live_private)


def has_conflict(ps: PairScore) -> bool:
    return any(r.status is EvidenceStatus.conflict and r.layer in SINGLE_VALUED_LAYERS for r in ps.rows)


def _pair(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


def cluster(
    nodes: Iterable[str],
    scores: Iterable[PairScore],
    tau: float,
    cannot_link: set[frozenset[str]] | frozenset[frozenset[str]] = frozenset(),
    must_link: Iterable[Iterable[str]] = (),
) -> list[set[str]]:
    """Partition `nodes` by greedy average linkage on signed weights `bits - tau` (see module doc).

    `must_link` groups start as one unit, split greedily where a pair inside is cannot-linked.
    Returns the clusters, largest first.
    """
    node_list = sorted(set(nodes))
    node_set = set(node_list)
    best: dict[frozenset[str], PairScore] = {}
    for ps in scores:
        if ps.a == ps.b or ps.a not in node_set or ps.b not in node_set:
            continue
        key = _pair(ps.a, ps.b)
        if key not in best or ps.bits > best[key].bits:
            best[key] = ps
    cannot = {p for p in cannot_link if len(p) == 2}

    # Units: must-link groups, split greedily where two members are cannot-linked.
    unit_of: dict[str, int] = {}
    units: list[list[str]] = []
    for group_ in must_link:
        local: list[int] = []
        for n in sorted(set(group_) & node_set):
            if n in unit_of:
                continue
            for ui in local:
                if not any(_pair(n, m) in cannot for m in units[ui]):
                    units[ui].append(n)
                    unit_of[n] = ui
                    break
            else:
                unit_of[n] = len(units)
                local.append(len(units))
                units.append([n])
    for n in node_list:
        if n not in unit_of:
            unit_of[n] = len(units)
            units.append([n])

    # Unit-to-unit weight: the best capture pair between them (D1: max over members).
    def weight(ps: PairScore) -> float:
        w = ps.bits - tau
        return w if qualifies(ps, tau) else min(w, 0.0)

    uw: dict[tuple[int, int], float] = {}
    uq: set[tuple[int, int]] = set()
    ucannot: set[tuple[int, int]] = set()
    for key, ps in best.items():
        ua, ub = unit_of[ps.a], unit_of[ps.b]
        if ua == ub:
            continue
        k = (min(ua, ub), max(ua, ub))
        uw[k] = max(uw.get(k, -math.inf), weight(ps))
        if qualifies(ps, tau):
            uq.add(k)
    for p in cannot:
        a, b = tuple(p)
        if a in unit_of and b in unit_of and unit_of[a] != unit_of[b]:
            ua, ub = unit_of[a], unit_of[b]
            ucannot.add((min(ua, ub), max(ua, ub)))

    # Greedy average linkage over units.
    clusters: dict[int, set[int]] = {i: {i} for i in range(len(units))}
    absent = -tau  # weight of a unit pair that shares nothing

    def cross(ca: set[int], cb: set[int]) -> tuple[float, bool, bool]:
        total, qual, blocked = 0.0, False, False
        for x in ca:
            for y in cb:
                k = (min(x, y), max(x, y))
                if k in ucannot:
                    blocked = True
                total += uw.get(k, absent)
                qual = qual or k in uq
        return total / (len(ca) * len(cb)), qual, blocked

    while True:
        cand: Optional[tuple[float, str, int, int]] = None
        # Only cluster pairs joined by a qualifying edge can merge.
        cluster_of = {u: c for c, us in clusters.items() for u in us}
        linked = {(min(cluster_of[x], cluster_of[y]), max(cluster_of[x], cluster_of[y])) for x, y in uq if cluster_of[x] != cluster_of[y]}
        for c1, c2 in sorted(linked):
            mean, qual, blocked = cross(clusters[c1], clusters[c2])
            if blocked or not qual or mean <= 0:
                continue
            tie = min(min(units[u]) for u in clusters[c1] | clusters[c2])
            key = (-mean, tie, c1, c2)
            if cand is None or key < cand:
                cand = key
        if cand is None:
            break
        _, _, c1, c2 = cand
        clusters[c1] |= clusters.pop(c2)

    out = [{n for u in cl for n in units[u]} for cl in clusters.values()]
    out.sort(key=lambda s: (-len(s), min(s)))
    return out


# --- dataset level -------------------------------------------------------------------------


def canonical_map(ds: Dataset) -> dict[str, str]:
    """Capture id -> the canonical capture it collapses onto (itself if not a duplicate)."""
    return {c.id: (c.duplicate_of or c.id) for i in ds.incidents for c in i.captures}


def candidate_pairs(ds: Dataset, relation: Relation, nodes: Optional[set[str]] = None) -> set[tuple[str, str]]:
    """Pairs of canonical captures that share at least one linkable value (plus single-valued buckets).

    Single-valued buckets (platform / tenant / owner) are only added for the relations with
    cannot-link, so the ledger gets to see the conflict.
    """
    canon = canonical_map(ds)
    postings: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    buckets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ind in ds.indicators():
        if not linkable(ind) or ind.value is None:
            continue
        cid = canon.get(ind.capture_id, ind.capture_id)
        if nodes is not None and cid not in nodes:
            continue
        postings[(ind.layer.value, ind.kind, ind.value)].add(cid)
        if ind.layer in SINGLE_VALUED_LAYERS:
            buckets[(ind.layer.value, ind.kind)].add(cid)
    pairs: set[tuple[str, str]] = set()
    lists = [p for p in postings.values() if 2 <= len(p) <= HUB_POSTINGS]
    if relation in CANNOT_LINK_RELATIONS:
        lists += [b for b in buckets.values() if len(b) >= 2]
    for members in lists:
        ms = sorted(members)
        for i, a in enumerate(ms):
            for b in ms[i + 1 :]:
                pairs.add((a, b))
    return pairs


def _resolve(ctx_or_backend: Any) -> Backend:
    return ctx_or_backend if isinstance(ctx_or_backend, Backend) else Backend.from_context(ctx_or_backend)


def link_id(relation: Relation, a: str, b: str, prefix: str = "link") -> str:
    a, b = sorted((a, b))
    return f"{prefix}:{relation.value}:{a}--{b}"


def group(ctx: Any, relation: Relation = Relation.campaign) -> tuple[list[Campaign], list[Link]]:
    """One partition of the collection under `relation`: campaigns plus the qualifying links inside them.

    `ctx` is a `ScoringContext` (HACK-21) or a `Backend`.
    """
    be = _resolve(ctx)
    ds = be.dataset
    canon = canonical_map(ds)
    captures = {c.id: c for i in ds.incidents for c in i.captures}
    nodes = {cid for cid, can in canon.items() if cid == can}
    tau = be.threshold(relation)

    scores = [be.score(a, b, relation) for a, b in sorted(candidate_pairs(ds, relation, nodes))]
    cannot: set[frozenset[str]] = set()
    if relation in CANNOT_LINK_RELATIONS:
        cannot = {_pair(ps.a, ps.b) for ps in scores if has_conflict(ps)}
    must = [[c.id for c in i.captures if c.id in nodes and c.stage is not Stage.decoy] for i in ds.incidents]

    clusters = cluster(nodes, scores, tau, cannot, must)

    by_pair = {_pair(ps.a, ps.b): ps for ps in scores}
    dups: dict[str, list[str]] = defaultdict(list)
    for cid, can in canon.items():
        if cid != can:
            dups[can].append(cid)
    incident_of = {c.id: i.id for i in ds.incidents for c in i.captures}

    campaigns: list[Campaign] = []
    links: list[Link] = []
    n_out = 0
    for cl in clusters:
        if all(captures[c].stage is Stage.decoy for c in cl) and len(cl) == 1:
            continue
        members = sorted(cl)
        cl_links: list[Link] = []
        layer_bits: dict[str, float] = defaultdict(float)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                ps = by_pair.get(_pair(a, b))
                if ps is None or not qualifies(ps, tau):
                    continue
                link = be.link(ps, link_id(relation, a, b))
                if link is None:
                    continue
                cl_links.append(link)
                for r in ps.rows:
                    if r.status is EvidenceStatus.shared and r.bits > 0:
                        layer_bits[r.layer.value] += r.bits
        n_out += 1
        capture_ids = sorted(set(members) | {d for m in members for d in dups.get(m, [])})
        incidents = sorted({incident_of[c] for c in capture_ids})
        campaigns.append(
            Campaign(
                id=f"campaign:{relation.value}:{n_out:03d}",
                label=_label(relation, capture_ids, incidents, layer_bits),
                relation=relation,
                capture_ids=capture_ids,
                link_ids=[l.id for l in cl_links],
            )
        )
        links.extend(cl_links)
    return campaigns, links


def _label(relation: Relation, capture_ids: Sequence[str], incidents: Sequence[str], layer_bits: dict[str, float]) -> str:
    if len(incidents) == 1:
        return f"{relation.value}: single incident {incidents[0]} ({len(capture_ids)} capture{'s' if len(capture_ids) != 1 else ''})"
    top = sorted(layer_bits.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    shared = ", ".join(k.replace("_", " ") for k, _ in top) or "no itemised layer"
    return f"{relation.value}: {len(incidents)} incidents, {len(capture_ids)} captures; shared {shared}"


def link_dataset(ds: Dataset, relations: Iterable[Relation] = ALL_RELATIONS, backend: Optional[Backend] = None) -> Dataset:
    """The input dataset with links and campaigns for every relation, validated.

    Links and campaigns already in the input are replaced (they belong to an earlier run or a
    fixture), and so are assignments, which point at those campaigns.
    """
    be = backend or Backend.from_dataset(ds)
    campaigns: list[Campaign] = []
    links: list[Link] = []
    for relation in dict.fromkeys(relations):
        c, l = group(be, relation)
        campaigns.extend(c)
        links.extend(l)
    data = ds.model_dump(mode="json")
    data["links"] = [l.model_dump(mode="json") for l in links]
    data["campaigns"] = [c.model_dump(mode="json") for c in campaigns]
    data["assignments"] = []
    return Dataset.model_validate(data)
