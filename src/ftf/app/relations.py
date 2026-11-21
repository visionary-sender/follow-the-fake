"""Campaign views per relation, as pure functions over a contract `Dataset` (HACK-31).

Each relation (same template, tracking account, operator, campaign, ecosystem) gets its own
graph of captures. An edge is a contract `Link` of that relation and carries only the evidence
rows with status `shared`. Collector-layer rows describe the analyst or the capture tool, not
the scam, so they never draw an edge and are kept apart as `excluded` for display.

Public functions:

- `relation_graph(ds, relation)`: networkx graph of captures for one relation.
- `clusters(ds, relation)`: campaigns of that relation plus unlabelled connected components.
- `campaign_summary(ds, relation=None)`: one row per campaign (sizes, top shared indicators,
  date span).
- `indicator_view(ds, campaign_id)`: bipartite capture ↔ shared-indicator graph (the pivots).
- `explain_pair(ds, a, b)`: every relation two captures are linked under, with the evidence.
- `edges(ds, relation)`, `evidence_rows(edge)`: drawable links and their compact evidence table.
- `tier(link_level, score_bits, threshold)`, `defang(text)`, `review_threshold_bits(ds)`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import networkx as nx

from ftf.contract import (
    Dataset,
    EvidenceRow,
    EvidenceStatus,
    Layer,
    Link,
    LinkLevel,
    Relation,
    linkable,
)

RELATIONS: tuple[Relation, ...] = tuple(Relation)

RELATION_LABEL: dict[Relation, str] = {
    Relation.template: "same template",
    Relation.tracking_account: "same tracking account",
    Relation.operator: "same operator",
    Relation.campaign: "same campaign",
    Relation.ecosystem: "same ecosystem",
}

# CERT Polska evidence tiers, the vocabulary NASK analysts use.
LEVEL_LABEL: dict[LinkLevel, str] = {
    LinkLevel.strict: "Strong",
    LinkLevel.medium: "Medium",
    LinkLevel.loose: "Supplementary",
}
INSUFFICIENT = "Insufficient alone"
TIERS: tuple[str, ...] = ("Strong", "Medium", "Supplementary", INSUFFICIENT)

COLLECTOR_NOTE = "describes the collector, not the scam"
MISSING_NOTE = "not available — not a penalty"


def review_threshold_bits(ds: Dataset) -> float:
    """Bits a single link needs to stand alone.

    Adapter until the linking engine publishes its threshold: τ = log2(n) over the captures in
    the base (the pairwise prior log-odds with campaigns of size 1, the conservative end of
    τ = log2(n/m) in the method notes). A link below τ is shown as "Insufficient alone".
    """
    n = sum(len(i.captures) for i in ds.incidents)
    return math.log2(max(n, 2))


def tier(level: LinkLevel, score_bits: float | None, threshold: float) -> str:
    """UI tier of a link: its level's label, or "Insufficient alone" below the threshold.

    An unscored link (e.g. hand-labelled) keeps its level's label: there is no score to compare.
    """
    if score_bits is not None and score_bits < threshold:
        return INSUFFICIENT
    return LEVEL_LABEL[level]


_SCHEME = re.compile(r"(?i)\bhttp(s?)(?=:|\b)")
# IDNA treats these as label separators too (a browser reads `a。b` as `a.b`).
_DOTS = "\u002e\u3002\uff0e\uff61"
# A host character: ASCII letter or digit, or any non-ASCII character that is not a space or a dot
# (IDN labels: letters, combining marks, joiners). Punycode labels (`xn--…`) are plain ASCII.
_HC = rf"(?:[a-z0-9]|[^\x00-\x7f\s{_DOTS}])"
_LABEL = rf"{_HC}(?:(?:{_HC}|-){{0,61}}{_HC})?"  # bounded: no quadratic backtracking on long strings
_TLD = rf"(?:[a-z]|[^\x00-\x7f\s{_DOTS}])(?:{_HC}|-){{1,62}}"
_HOSTISH = re.compile(rf"(?i)(?<!{_HC})(?<!-)(?:{_LABEL}[{_DOTS}])+{_TLD}(?!{_HC}|-)")
_DOT_RE = re.compile(f"[{_DOTS}]")


def defang(text: str | None) -> str:
    """Make URLs and domains non-clickable: `http` → `hxxp`, dots in host names → `[.]`.

    Covers IDN hosts written in Unicode (any script) and their full-width / ideographic dots.
    """
    if text is None:
        return ""
    text = _SCHEME.sub(lambda m: "hxxp" + m.group(1), text)
    return _HOSTISH.sub(lambda m: _DOT_RE.sub("[.]", m.group(0)), text)


@dataclass(frozen=True)
class Shared:
    """One shared indicator on a link (an evidence row with status `shared`)."""

    layer: str
    kind: str
    value: str | None
    bits: float
    seen_in: str | None = None
    note: str | None = None

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.layer, self.kind, self.value)


@dataclass(frozen=True)
class Edge:
    """A drawable link: one contract `Link` reduced to what the views need."""

    link_id: str
    a: str
    b: str
    relation: Relation
    level: LinkLevel
    score_bits: float | None  # None: the link carries no score (e.g. hand-labelled)
    tier: str
    method: str
    shared: tuple[Shared, ...]
    excluded: tuple[EvidenceRow, ...] = ()  # collector rows, shown greyed, never an edge
    other: tuple[EvidenceRow, ...] = ()  # conflict / missing / hub_subsumed rows

    @property
    def bits(self) -> float:
        """Score for sorting and edge width; an unscored link counts as 0."""
        return self.score_bits if self.score_bits is not None else 0.0

    @property
    def bits_text(self) -> str:
        return "n/a" if self.score_bits is None else f"{self.score_bits:.1f}"


@dataclass(frozen=True)
class Cluster:
    id: str
    label: str
    relation: Relation
    capture_ids: tuple[str, ...]
    link_ids: tuple[str, ...]
    source: str  # "campaign" (from the base) or "component" (unlabelled connected component)


@dataclass(frozen=True)
class TopIndicator:
    layer: str
    kind: str
    value: str | None
    links: int  # number of the campaign's links that share it
    max_bits: float


@dataclass(frozen=True)
class CampaignSummary:
    campaign_id: str
    label: str
    relation: Relation
    n_captures: int
    n_incidents: int
    n_links: int
    top_indicators: tuple[TopIndicator, ...]
    first_seen: str | None
    last_seen: str | None


@dataclass(frozen=True)
class PairExplanation:
    """Why two captures are linked under one relation."""

    relation: Relation
    label: str
    direct: bool  # True: a link joins the two captures; False: a path through other captures
    path: tuple[str, ...]  # capture ids from a to b
    edges: tuple[Edge, ...]  # the edges along the path (all direct links when direct)
    campaign_ids: tuple[str, ...] = field(default=())  # campaigns of this relation holding both


def _linkable_ids(ds: Dataset) -> set[str]:
    return {ind.id for ind in ds.indicators() if linkable(ind)}


def _row_draws(row: EvidenceRow, ok_ids: set[str]) -> bool:
    """A row can carry an edge only if it is shared, not collector, and all its indicators are linkable."""
    return (
        row.status is EvidenceStatus.shared
        and row.layer is not Layer.collector
        and all(i in ok_ids for i in row.indicator_ids)
    )


def to_edge(link: Link, threshold: float, ok_ids: set[str]) -> Edge | None:
    """Reduce a link to an `Edge`; None when no shared, non-collector evidence remains."""
    shared, excluded, other = [], [], []
    for row in link.evidence:
        if _row_draws(row, ok_ids):
            shared.append(Shared(row.layer.value, row.kind, row.value, row.bits, row.seen_in, row.note))
        elif row.layer is Layer.collector or row.status is EvidenceStatus.collector_excluded:
            excluded.append(row)
        else:
            other.append(row)
    if not shared:
        return None
    shared.sort(key=lambda s: -s.bits)
    return Edge(
        link_id=link.id,
        a=link.a,
        b=link.b,
        relation=link.relation,
        level=link.level,
        score_bits=link.score_bits,
        tier=tier(link.level, link.score_bits, threshold),
        method=link.method,
        shared=tuple(shared),
        excluded=tuple(excluded),
        other=tuple(other),
    )


def edges(ds: Dataset, relation: Relation | None = None, threshold: float | None = None) -> list[Edge]:
    """All drawable edges, optionally for one relation."""
    if threshold is None:
        threshold = review_threshold_bits(ds)
    ok = _linkable_ids(ds)
    out = []
    for link in ds.links:
        if relation is not None and link.relation is not relation:
            continue
        e = to_edge(link, threshold, ok)
        if e is not None:
            out.append(e)
    return out


def _capture_meta(ds: Dataset) -> dict[str, dict]:
    meta = {}
    for inc in ds.incidents:
        for c in inc.captures:
            meta[c.id] = {
                "incident_id": inc.id,
                "stage": c.stage.value,
                "captured_on": c.captured_on,
                "duplicate_of": c.duplicate_of,
            }
    return meta


def relation_graph(ds: Dataset, relation: Relation, threshold: float | None = None) -> nx.Graph:
    """Graph of captures under one relation.

    Nodes: captures that sit in a campaign of this relation or touch one of its edges, with
    `incident_id`, `stage`, `captured_on`, `campaign_ids`. Edges: attribute `edges`, the list of
    `Edge`s joining the pair (usually one), plus `bits` (the strongest) and `tier` (of the strongest).
    """
    rel = Relation(relation)
    meta = _capture_meta(ds)
    camps = [c for c in ds.campaigns if c.relation is rel]
    member: dict[str, list[str]] = defaultdict(list)
    for c in camps:
        for cid in c.capture_ids:
            member[cid].append(c.id)
    g = nx.Graph(relation=rel)

    def add_node(cid: str) -> None:
        if cid not in g:
            g.add_node(cid, **meta[cid], campaign_ids=tuple(member.get(cid, ())))

    for cid in member:
        add_node(cid)
    for e in edges(ds, rel, threshold):
        add_node(e.a)
        add_node(e.b)
        if g.has_edge(e.a, e.b):
            g[e.a][e.b]["edges"].append(e)
        else:
            g.add_edge(e.a, e.b, edges=[e])
        best = max(g[e.a][e.b]["edges"], key=lambda x: x.bits)
        g[e.a][e.b]["bits"] = best.bits
        g[e.a][e.b]["tier"] = best.tier
    return g


def clusters(ds: Dataset, relation: Relation, threshold: float | None = None) -> list[Cluster]:
    """Campaigns of this relation, then connected components no campaign covers."""
    rel = Relation(relation)
    g = relation_graph(ds, rel, threshold)
    out = [
        Cluster(c.id, c.label, rel, tuple(c.capture_ids), tuple(_campaign_link_ids(ds, c, g)), "campaign")
        for c in ds.campaigns
        if c.relation is rel
    ]
    covered = [set(c.capture_ids) for c in out]
    n = 0
    for comp in sorted(nx.connected_components(g), key=lambda s: (-len(s), sorted(s))):
        if len(comp) < 2 or any(comp <= cov for cov in covered):
            continue
        n += 1
        sub = g.subgraph(comp)
        lids = sorted({e.link_id for _, _, d in sub.edges(data=True) for e in d["edges"]})
        out.append(Cluster(f"{rel.value}:component-{n}", f"Unlabelled group {n}", rel,
                           tuple(sorted(comp)), tuple(lids), "component"))
    return out


def _campaign_link_ids(ds: Dataset, campaign, g: nx.Graph | None = None) -> list[str]:
    """The campaign's own links of its relation with both ends inside it, else the drawable
    links of its relation inside it."""
    if campaign.link_ids:
        inside = set(campaign.capture_ids)
        by_id = {l.id: l for l in ds.links}
        return [lid for lid in campaign.link_ids
                if by_id[lid].relation is campaign.relation and {by_id[lid].a, by_id[lid].b} <= inside]
    if g is None:
        g = relation_graph(ds, campaign.relation)
    inside = set(campaign.capture_ids)
    sub = g.subgraph([c for c in inside if c in g])
    return sorted({e.link_id for _, _, d in sub.edges(data=True) for e in d["edges"]})


def campaign_summary(ds: Dataset, relation: Relation | None = None, top: int = 5) -> list[CampaignSummary]:
    """One row per campaign: label, relation, #captures, #incidents, top shared indicators, date span."""
    meta = _capture_meta(ds)
    threshold = review_threshold_bits(ds)
    ok = _linkable_ids(ds)
    by_id = {l.id: l for l in ds.links}
    rows = []
    for c in ds.campaigns:
        if relation is not None and c.relation is not Relation(relation):
            continue
        agg: dict[tuple, list] = {}
        lids = _campaign_link_ids(ds, c)
        for lid in lids:
            e = to_edge(by_id[lid], threshold, ok)
            if e is None:
                continue
            for s in e.shared:
                cur = agg.setdefault(s.key, [0, 0.0])
                cur[0] += 1
                cur[1] = max(cur[1], s.bits)
        tops = sorted(
            (TopIndicator(k[0], k[1], k[2], v[0], v[1]) for k, v in agg.items()),
            key=lambda t: (-t.max_bits, -t.links, t.layer, t.kind, t.value or ""),
        )[:top]
        dates = sorted(d for d in (meta[x]["captured_on"] for x in c.capture_ids) if d)
        rows.append(CampaignSummary(
            campaign_id=c.id,
            label=c.label,
            relation=c.relation,
            n_captures=len(c.capture_ids),
            n_incidents=len({meta[x]["incident_id"] for x in c.capture_ids}),
            n_links=len(lids),
            top_indicators=tuple(tops),
            first_seen=dates[0] if dates else None,
            last_seen=dates[-1] if dates else None,
        ))
    return rows


def indicator_view(ds: Dataset, campaign_id: str, min_captures: int = 2) -> nx.Graph:
    """Bipartite capture ↔ indicator graph for one campaign: the pivots that join it.

    Indicator nodes are ("ind", layer, kind, value) for linkable indicators with a value that at
    least `min_captures` of the campaign's captures carry. Collector-layer indicators and
    embeddings (vector references) are skipped.
    """
    camp = next((c for c in ds.campaigns if c.id == campaign_id), None)
    if camp is None:
        raise KeyError(campaign_id)
    caps = set(camp.capture_ids)
    meta = _capture_meta(ds)
    holders: dict[tuple, set[str]] = defaultdict(set)
    for ind in ds.indicators():
        if ind.capture_id in caps and linkable(ind) and ind.value is not None:
            holders[("ind", ind.layer.value, ind.kind, ind.value)].add(ind.capture_id)
    g = nx.Graph(campaign_id=campaign_id)
    for cid in sorted(caps):
        g.add_node(cid, side="capture", **meta[cid])
    for key, cids in holders.items():
        if len(cids) < min_captures:
            continue
        g.add_node(key, side="indicator", layer=key[1], kind=key[2], value=key[3], n=len(cids))
        for cid in cids:
            g.add_edge(cid, key)
    return g


def explain_pair(ds: Dataset, a: str, b: str, threshold: float | None = None) -> list[PairExplanation]:
    """Every relation `a` and `b` are linked under, with the shared evidence.

    Direct links come first. Without one, the strongest path between them in that relation's
    graph is returned (inside a common campaign when they share one), so the viewer sees each
    hop's shared indicators.
    """
    meta = _capture_meta(ds)
    for x in (a, b):
        if x not in meta:
            raise KeyError(x)
    out = []
    if a == b:
        return out
    for rel in RELATIONS:
        g = relation_graph(ds, rel, threshold)
        common = tuple(c.id for c in ds.campaigns
                       if c.relation is rel and a in c.capture_ids and b in c.capture_ids)
        if g.has_edge(a, b):
            out.append(PairExplanation(rel, RELATION_LABEL[rel], True, (a, b),
                                       tuple(g[a][b]["edges"]), common))
            continue
        if a not in g or b not in g:
            continue
        search = g
        if common:
            inside = set().union(*(set(c.capture_ids) for c in ds.campaigns if c.id in common))
            search = g.subgraph(inside)
        path = _strongest_path(search, a, b) or (_strongest_path(g, a, b) if search is not g else None)
        if path is None:
            continue
        hops = tuple(max(g[u][v]["edges"], key=lambda e: e.bits) for u, v in zip(path, path[1:]))
        out.append(PairExplanation(rel, RELATION_LABEL[rel], False, tuple(path), hops, common))
    return out


def evidence_rows(edge: Edge) -> list[dict]:
    """Compact evidence table for one edge: shared rows, then other rows, then collector rows.

    Values are defanged. Follow-up: switch to `ftf.app.ledger` (HACK-30) once it is on main.
    """
    rows = [
        {"evidence": "shared", "layer": s.layer, "kind": s.kind, "value": defang(s.value),
         "bits": round(s.bits, 2), "seen in": s.seen_in or "", "note": s.note or ""}
        for s in edge.shared
    ]
    for r in edge.other:
        note = MISSING_NOTE if r.status is EvidenceStatus.missing else (r.note or "")
        rows.append({"evidence": r.status.value, "layer": r.layer.value, "kind": r.kind,
                     "value": defang(r.value), "bits": round(r.bits, 2), "seen in": r.seen_in or "",
                     "note": note})
    for r in edge.excluded:
        rows.append({"evidence": "collector (excluded)", "layer": r.layer.value, "kind": r.kind,
                     "value": defang(r.value), "bits": 0.0, "seen in": r.seen_in or "",
                     "note": COLLECTOR_NOTE})
    return rows


def _strongest_path(g: nx.Graph, a: str, b: str) -> list[str] | None:
    """Fewest hops between `a` and `b`, ties broken towards stronger links."""
    if a not in g or b not in g:
        return None
    try:
        return nx.shortest_path(g, a, b, weight=lambda u, v, d: 1.0 + 1.0 / (1.0 + max(d["bits"], 0.0)))
    except nx.NetworkXNoPath:
        return None
