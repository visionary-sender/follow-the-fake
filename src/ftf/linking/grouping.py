"""One partition of the captures per relation, with cannot-link on conflicting tenant / owner ids.

Method (07 §1.8, D2 "Linking / clustering"):

* Candidate pairs come from an inverted index over linkable indicator values: two captures are
  scored only if they share at least one value (compared in the ledger's normalised form), or
  (operator / campaign relations) both carry a tenant or owner id so the ledger can report a
  conflict. Cannot-link = a tenant/owner conflict (`ledger.has_private_conflict`); a platform
  conflict only costs bits, since one operator can run several platforms.
* An edge qualifies when bits >= tau and (>= `MIN_LAYERS[relation]` positive layers or a live
  operator-private id). Operator, campaign and tracking-account links need two layers, so one
  copied static id cannot make them. Template and ecosystem need one: a kit is by itself the
  evidence of a shared template, and the ledger gives them no private-id layers to agree with.
  The signed weight of a pair is `bits - tau` for qualifying edges, `min(bits - tau, 0)`
  otherwise, and `-tau` for a pair that shares nothing (0 bits).
* Clustering is greedy average linkage on those signed weights (GAEC-style correlation
  clustering): repeatedly merge the two clusters with the highest mean cross weight while that
  mean is non-negative, they are joined by at least one qualifying edge, and no cross pair is
  cannot-linked. Unlike connected components, one false private-id match between two groups
  is outvoted by all the other cross pairs, so it does not chain them.
* Must-link only where an incident's redirect chain is unambiguous (HACK-57). The chain's
  end points are its non-decoy lander (or unknown-stage) captures. With at most one end point,
  all non-decoy captures of the incident (ad, pre-lander, lander) start in one unit, unless two
  of them conflict. With several end points the incident is not one chain: a case folder can
  hold unrelated sites, so its landers join only through their own evidence, like any other
  pair. Its ad / pre-lander captures are then attached to the lander they share the most
  evidence with (the highest-bit pair with bits > 0, ties by id); without any such evidence
  they stay their own unit. Between units the best capture pair counts
  (D1: max over members). A decoy capture is what the cloaker shows non-targets, so it is
  never must-linked to its incident. Under operator / campaign it clusters only with other
  decoys (cannot-link to non-decoys), so it cannot pull its incident into a second campaign;
  under template / tracking account / ecosystem it joins a cluster on its own evidence.
  Must-link groups use canonical capture ids, so an incident whose capture duplicates another
  incident's capture stays in one piece (and joins that incident).
* Every cluster with at least one non-decoy capture (counting duplicates) becomes a
  `Campaign`, singletons included, so each relation's campaigns are a full partition of the
  non-decoy captures and assignment can match a new item to a collection item that has no
  group yet. Decoy-only clusters, of any size, are left out. Exact duplicates (`duplicate_of`) follow their canonical capture.
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
    Incident,
    Layer,
    Link,
    Relation,
    Stage,
    linkable,
)
from ftf.linking.backend import Backend
from ftf.linking.ledger import has_private_conflict, normalise
from ftf.linking.types import PairScore
from ftf.linking.weights import IGNORED_VALUES

ALL_RELATIONS: tuple[Relation, ...] = tuple(Relation)
SINGLE_VALUED_LAYERS = frozenset({Layer.platform, Layer.tenant, Layer.owner})  # conflicts shown in notes
PRIVATE_SINGLE_LAYERS = frozenset({Layer.tenant, Layer.owner})  # conflicts that are cannot-link
CANNOT_LINK_RELATIONS = frozenset({Relation.operator, Relation.campaign})
# Stages that end a redirect chain: an incident with several of them is not one ad's chain.
END_STAGES = frozenset({Stage.lander, Stage.unknown})
MIN_LAYERS: dict[Relation, int] = {
    Relation.template: 1,
    Relation.tracking_account: 2,
    Relation.operator: 2,
    Relation.campaign: 2,
    Relation.ecosystem: 1,
}
# A value carried by more captures than this is a hub: it carries ~0 bits, so it never proposes pairs.
HUB_POSTINGS = 500


def qualifies(ps: PairScore, tau: float, min_layers: int = 2) -> bool:
    return ps.bits >= tau and ps.layers_positive >= 1 and (ps.layers_positive >= min_layers or ps.live_private)


def has_conflict(ps: PairScore) -> bool:
    """Cannot-link: a tenant or owner conflict (not platform)."""
    return has_private_conflict(ps)


def _pair(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


def cluster(
    nodes: Iterable[str],
    scores: Iterable[PairScore],
    tau: float,
    cannot_link: set[frozenset[str]] | frozenset[frozenset[str]] = frozenset(),
    must_link: Iterable[Iterable[str]] = (),
    min_layers: int = 2,
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

    # Units: must-link groups (overlapping groups are joined), split greedily where two members
    # are cannot-linked.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for group_ in must_link:
        ms = sorted(set(group_) & node_set)
        for m in ms[1:]:
            ra, rb = find(ms[0]), find(m)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    joined: dict[str, list[str]] = defaultdict(list)
    for n in sorted(parent):
        joined[find(n)].append(n)
    unit_of: dict[str, int] = {}
    units: list[list[str]] = []
    for root in sorted(joined):
        local: list[int] = []
        for n in joined[root]:
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
        return w if qualifies(ps, tau, min_layers) else min(w, 0.0)

    uw: dict[tuple[int, int], float] = {}
    uq: set[tuple[int, int]] = set()
    ucannot: set[tuple[int, int]] = set()
    for key, ps in best.items():
        ua, ub = unit_of[ps.a], unit_of[ps.b]
        if ua == ub:
            continue
        k = (min(ua, ub), max(ua, ub))
        uw[k] = max(uw.get(k, -math.inf), weight(ps))
        if qualifies(ps, tau, min_layers):
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
            if blocked or not qual or mean < 0:
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


def incident_chain(incident: Incident, canon: dict[str, str], stage: dict[str, Stage]) -> tuple[list[str], list[str]]:
    """(end points, the rest) among an incident's canonical non-decoy captures, in capture order.

    End points are the lander / unknown-stage captures; the rest are ad and pre-lander captures.
    The chain is unambiguous, and may be must-linked whole, only with at most one end point.
    A capture counts as a decoy if it or its canonical capture is one.
    """
    caps = list(dict.fromkeys(canon[c.id] for c in incident.captures if c.stage is not Stage.decoy and stage[canon[c.id]] is not Stage.decoy))
    ends = [c for c in caps if stage[c] in END_STAGES]
    return ends, [c for c in caps if c not in ends]


def must_link_groups(ds: Dataset, by_pair: dict[frozenset[str], PairScore]) -> list[list[str]]:
    """Must-link groups of canonical capture ids, one or more per incident (see module doc)."""
    canon = canonical_map(ds)
    stage = {c.id: c.stage for i in ds.incidents for c in i.captures}
    groups: list[list[str]] = []
    for inc in ds.incidents:
        ends, rest = incident_chain(inc, canon, stage)
        if len(ends) <= 1:
            groups.append(ends + rest)
            continue
        for x in rest:
            best: Optional[tuple[float, str]] = None
            for e in ends:
                ps = by_pair.get(_pair(x, e))
                if ps is not None and ps.bits > 0 and (best is None or (-ps.bits, e) < (-best[0], best[1])):
                    best = (ps.bits, e)
            if best is not None:
                groups.append([best[1], x])
    return groups


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
        norm = normalise(ind.value, ind.layer)
        if not norm or norm.casefold() in IGNORED_VALUES:
            continue
        postings[(ind.layer.value, ind.kind, norm)].add(cid)
        if ind.layer in PRIVATE_SINGLE_LAYERS:
            buckets[(ind.layer.value, ind.kind)].add(cid)
    pairs: set[tuple[str, str]] = set()
    lists = [p for p in postings.values() if 2 <= len(p) <= HUB_POSTINGS]
    if relation in CANNOT_LINK_RELATIONS:
        # Capped like value postings: past the cap the conflict is only seen between captures
        # that share some other value (pairs that share nothing never link anyway).
        lists += [b for b in buckets.values() if 2 <= len(b) <= HUB_POSTINGS]
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
        # Decoys cluster only with decoys here: what a cloaker shows non-targets says nothing
        # about who runs the campaign, and must not pull its incident into a second one.
        decoys = {n for n in nodes if captures[n].stage is Stage.decoy}
        cannot |= {_pair(ps.a, ps.b) for ps in scores if (ps.a in decoys) != (ps.b in decoys)}
    by_pair = {_pair(ps.a, ps.b): ps for ps in scores}
    must = must_link_groups(ds, by_pair)

    min_layers = MIN_LAYERS[relation]
    clusters = cluster(nodes, scores, tau, cannot, must, min_layers)

    dups: dict[str, list[str]] = defaultdict(list)
    for cid, can in canon.items():
        if cid != can:
            dups[can].append(cid)
    incident_of = {c.id: i.id for i in ds.incidents for c in i.captures}

    campaigns: list[Campaign] = []
    links: list[Link] = []
    n_out = 0
    for cl in clusters:
        members = sorted(cl)
        with_dups = set(members) | {d for m in members for d in dups.get(m, [])}
        if all(captures[c].stage is Stage.decoy for c in with_dups):
            continue
        cl_links: list[Link] = []
        layer_bits: dict[str, float] = defaultdict(float)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                ps = by_pair.get(_pair(a, b))
                if ps is None or not qualifies(ps, tau, min_layers):
                    continue
                link = be.link(ps, link_id(relation, a, b))
                if link is None:
                    continue
                cl_links.append(link)
                for r in ps.rows:
                    if r.status is EvidenceStatus.shared and r.bits > 0:
                        layer_bits[r.layer.value] += r.bits
        n_out += 1
        capture_ids = sorted(with_dups)
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
