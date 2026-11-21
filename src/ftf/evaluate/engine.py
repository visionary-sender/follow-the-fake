"""Engine adapter: the harness calls engines only through this shape (Linking's API, HACK-21).

- `link_dataset(ds) -> Dataset`: the input with `links` and `campaigns` filled in, per relation.
- `link_incident(base, incident, relation=Relation.campaign) -> IncidentResult`: open-set
  assignment of one new incident against a linked base (base first), without re-clustering.

`BaselineEngine` is the naive "loose" linker: connected components over any shared linkable
(non-collector) value. It is expected to blob; it exists so the harness runs end to end and so
real engines have a floor to beat. `get_engine("linking")` wraps `ftf.linking` when installed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from ftf.contract import (
    Assignment,
    Campaign,
    Candidate,
    Dataset,
    Decision,
    EvidenceRow,
    EvidenceStatus,
    Incident,
    Indicator,
    Link,
    LinkLevel,
    Relation,
    linkable,
)

try:  # Linking's result type, when its package is on main
    from ftf.linking import IncidentResult  # type: ignore[import-not-found,attr-defined]
except ImportError:

    @dataclass
    class IncidentResult:  # type: ignore[no-redef]
        assignment: Assignment
        links: list[Link] = field(default_factory=list)


class Engine(Protocol):
    def link_dataset(self, ds: Dataset) -> Dataset: ...

    def link_incident(self, base: Dataset, incident: Incident, relation: Relation = Relation.campaign) -> IncidentResult: ...


BASELINE_METHOD = "baseline-shared-value@0.1"


def _values(indicators: list[Indicator]) -> dict[tuple[str, str], list[Indicator]]:
    """(kind, value) -> indicators, linkable value indicators only."""
    out: dict[tuple[str, str], list[Indicator]] = defaultdict(list)
    for ind in indicators:
        if ind.value is not None and linkable(ind):
            out[(ind.kind, ind.value)].append(ind)
    return out


def _row(inds: list[Indicator], note: str) -> EvidenceRow:
    first = inds[0]
    return EvidenceRow(
        layer=first.layer, kind=first.kind, status=EvidenceStatus.shared, value=first.value[:120] if first.value else None,
        bits=1.0, indicator_ids=[i.id for i in inds], note=note,
    )


class BaselineEngine:
    """Connected components over any shared linkable value; the same partition for every relation."""

    name = "baseline"

    def link_dataset(self, ds: Dataset) -> Dataset:
        caps = [c.id for inc in ds.incidents for c in inc.captures]
        parent = {c: c for c in caps}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        edges: list[tuple[str, str, list[Indicator]]] = []  # spanning-forest edges with their evidence
        for (_, _), inds in sorted(_values(ds.indicators()).items()):
            by_cap: dict[str, Indicator] = {}
            for i in inds:
                by_cap.setdefault(i.capture_id, i)
            ordered = sorted(by_cap)
            for other in ordered[1:]:
                a, b = find(ordered[0]), find(other)
                if a != b:
                    parent[b] = a
                    edges.append((ordered[0], other, [by_cap[ordered[0]], by_cap[other]]))
        comps: dict[str, list[str]] = defaultdict(list)
        for c in caps:
            comps[find(c)].append(c)

        links, campaigns = [], []
        for rel in Relation:
            link_of: dict[str, list[str]] = defaultdict(list)
            for n, (a, b, inds) in enumerate(edges):
                lid = f"baseline:{rel.value}:L{n}"
                links.append(Link(id=lid, a=a, b=b, relation=rel, level=LinkLevel.loose, score_bits=1.0,
                                  method=BASELINE_METHOD, evidence=[_row(inds, "first shared value")]))
                link_of[find(a)].append(lid)
            for n, (root, members) in enumerate(sorted(comps.items(), key=lambda kv: sorted(kv[1])[0])):
                campaigns.append(Campaign(id=f"baseline:{rel.value}:C{n}", label=f"component {n}", relation=rel,
                                          capture_ids=sorted(members), link_ids=link_of[root]))
        out = ds.model_copy(update={"links": links, "campaigns": campaigns, "assignments": []})
        return Dataset.model_validate(out.model_dump())

    def link_incident(self, base: Dataset, incident: Incident, relation: Relation = Relation.campaign) -> IncidentResult:
        base_values = _values(base.indicators())
        new_values = _values(incident.indicators)
        camp_of = {cid: c for c in base.campaigns if c.relation is relation for cid in c.capture_ids}
        score: dict[str, float] = defaultdict(float)
        best: dict[str, tuple[str, str, list[Indicator]]] = {}
        for key, inds in new_values.items():
            for other in base_values.get(key, []):
                camp = camp_of.get(other.capture_id)
                if camp is None:
                    continue
                score[camp.id] += 1.0
                best.setdefault(camp.id, (inds[0].capture_id, other.capture_id, [inds[0], other]))
        cands = [Candidate(campaign_id=cid, score_bits=s) for cid, s in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))]
        capacity = float(len(new_values))
        if cands:
            top = cands[0].campaign_id
            a, b, inds = best[top]
            link = Link(id=f"baseline:assign:{incident.id}", a=a, b=b, relation=relation, level=LinkLevel.loose,
                        score_bits=cands[0].score_bits, method=BASELINE_METHOD, evidence=[_row(inds, "first shared value")])
            assignment = Assignment(incident_id=incident.id, candidates=cands, decision=Decision.assign, campaign_id=top,
                                    evidence_capacity_bits=capacity, method=BASELINE_METHOD)
            return IncidentResult(assignment=assignment, links=[link])
        assignment = Assignment(incident_id=incident.id, candidates=[], decision=Decision.new,
                                evidence_capacity_bits=capacity, method=BASELINE_METHOD)
        return IncidentResult(assignment=assignment, links=[])


class LinkingEngine:
    """Thin wrapper over `ftf.linking` (HACK-21/22)."""

    name = "linking"

    def __init__(self) -> None:
        try:
            import ftf.linking as linking  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "engine 'linking' needs the ftf.linking package (Linking engine, HACK-21/22), which is not "
                "installed in this checkout yet; use --engine baseline"
            ) from exc
        missing = [f for f in ("link_dataset", "link_incident") if not hasattr(linking, f)]
        if missing:
            raise RuntimeError(f"ftf.linking does not export {', '.join(missing)} yet (API posted on HACK-21)")
        self._linking = linking

    def link_dataset(self, ds: Dataset) -> Dataset:
        return self._linking.link_dataset(ds)

    def link_incident(self, base: Dataset, incident: Incident, relation: Relation = Relation.campaign) -> IncidentResult:
        return self._linking.link_incident(base, incident, relation=relation)


ENGINES = {"baseline": BaselineEngine, "linking": LinkingEngine}


def get_engine(name: str) -> Engine:
    try:
        factory = ENGINES[name]
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; choose one of {', '.join(sorted(ENGINES))}") from None
    return factory()
