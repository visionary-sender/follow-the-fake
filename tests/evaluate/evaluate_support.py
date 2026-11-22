"""Tiny synthetic dataset and a fake linking engine for the evaluation tests.

The fake engine links two captures under `campaign` when they share a value in an identity
layer, and under `template` when they share a kit value. Options make it misbehave on purpose
(use collector fields, link on faces) so the tests can show the checks catch it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ftf.contract import (
    Assignment,
    Campaign,
    Candidate,
    Dataset,
    Decision,
    EvidenceRow,
    EvidenceStatus,
    Incident,
    Layer,
    Level,
    Link,
    LinkLevel,
    Relation,
)

IDENTITY = {Layer.tenant, Layer.tracking_account, Layer.landing, Layer.owner, Layer.tracker_instance}
BITS = 10.0

# artefact type -> (artefact suffix, list of (layer, kind, level, value-template))
_SPEC = {
    "screenshot": [("creative", "face_image", "ecosystem", "face-celebrity")],
    "url": [("landing", "landing_path", "landing", "https://cdn.example/{tenant}/lp{g}x123456/index")],
    "html": [
        ("tenant", "tenant_id", "tenant", "{tenant}"),
        ("kit", "kit_hash", "operator", "kit-shared-0001"),
        ("platform", "platform_sdk", "platform", "sdk-main-js"),
    ],
    "har": [("tracking_account", "fb_pixel", "operator", "{pixel}")],
    "text": [("offer", "ad_text_simhash", "campaign", "text-{g}")],
}


def incident(iid: str, caps: list[tuple[str, str]], *, dup_of: dict[str, str] | None = None) -> dict:
    """caps: (capture id, group letter). Group letter g sets tenant/pixel/landing values."""
    captures, indicators = [], []
    for cid, g in caps:
        tenant = {"A": "a1a1a1a1", "B": "b2b2b2b2", "C": "c3c3c3c3", "D": "d4d4d4d4"}.get(g, "abcdef12")
        pixel = {"A": "5100000000001", "B": "5200000000002", "C": "5300000000003", "D": "5400000000004"}.get(g, "0")
        arts = []
        for t, specs in _SPEC.items():
            aid = f"{cid}:{t}"
            arts.append({"id": aid, "type": t, "path": f"raw/{cid}/{t}", "sha256": "0" * 64, "size": 1})
            for n, (layer, kind, level, tmpl) in enumerate(specs):
                indicators.append({
                    "id": f"{cid}:{t}:{n}",
                    "capture_id": cid,
                    "layer": layer,
                    "kind": kind,
                    "level": level,
                    "value": tmpl.format(tenant=tenant, pixel=pixel, g=g),
                    "observed": "live" if t == "har" else "static",
                    "provenance": {"artefact_id": aid},
                    "extractor": "test@0",
                })
        indicators.append({
            "id": f"{cid}:viewport", "capture_id": cid, "layer": "collector", "kind": "viewport",
            "level": "capture", "value": f"vp-{cid}", "observed": "static", "extractor": "test@0",
        })
        captures.append({
            "id": cid, "stage": "lander", "captured_on": "2026-10-08", "artefacts": arts,
            "missing": [{"modality": "video", "reason": "not captured"}],
            "duplicate_of": (dup_of or {}).get(cid),
        })
    return {"id": iid, "source": "test", "captures": captures, "indicators": indicators}


def dataset() -> Dataset:
    """Groups: A = a1, a2, a3 (one per incident) + mix/a; B = b1, b2 + mix/b; C = c1 alone;
    D = d1, d2 where d2 is an exact duplicate of d1 (separate incidents)."""
    incs = [
        incident("ia1", [("a1", "A")]),
        incident("ia2", [("a2", "A")]),
        incident("ia3", [("a3", "A")]),
        incident("ib1", [("b1", "B")]),
        incident("ib2", [("b2", "B")]),
        incident("ic1", [("c1", "C")]),
        incident("imix", [("mix/a", "A"), ("mix/b", "B")]),
        incident("id1", [("d1", "D")]),
        incident("id2", [("d2", "D")], dup_of={"d2": "d1"}),
    ]
    return Dataset.model_validate({"name": "synthetic", "incidents": incs})


GROUPS = {
    "A": ["a1", "a2", "a3", "mix/a"],
    "B": ["b1", "b2", "mix/b"],
    "C": ["c1"],
    "D": ["d1", "d2"],
}


@dataclass
class Result:
    assignment: Assignment
    links: list[Link]


class FakeEngine:
    """Links on shared identity values (campaign) and kit values (template); platform- and
    ecosystem-level values never link unless `link_platform` (creative faces only with `link_faces`)."""

    def __init__(self, *, use_collector: bool = False, link_faces: bool = False, link_platform: bool = False,
                 threshold: float = BITS):
        self.use_collector = use_collector
        self.link_platform = link_platform
        self.link_faces = link_faces
        self.threshold = threshold
        self.calls = 0

    def _campaign_layers(self):
        layers = set(IDENTITY)
        if self.use_collector:
            layers.add(Layer.collector)
        if self.link_faces:
            layers.add(Layer.creative)
        return layers

    def _keys(self, inc: Incident, cid: str, layers) -> dict:
        coarse = () if self.link_platform else (Level.platform, Level.ecosystem)
        return {
            (i.layer, i.kind, i.value): i for i in inc.indicators
            if i.capture_id == cid and i.value is not None and i.layer in layers
            and (i.level not in coarse or i.layer is Layer.creative)
        }

    def _link(self, n, a_inc, a, b_inc, b, layers, relation) -> Link | None:
        ka, kb = self._keys(a_inc, a, layers), self._keys(b_inc, b, layers)
        shared = sorted(set(ka) & set(kb), key=str)
        if not shared:
            return None
        rows = []
        for key in shared:
            layer, kind, _ = key
            collector = layer is Layer.collector
            rows.append(EvidenceRow(
                layer=layer, kind=kind,
                status=EvidenceStatus.collector_excluded if collector else EvidenceStatus.shared,
                bits=0.0 if collector else BITS,
                indicator_ids=[] if collector else [ka[key].id, kb[key].id],
            ))
        return Link(
            id=f"l{n}", a=a, b=b, relation=relation, level=LinkLevel.strict,
            score_bits=BITS * len(shared), method="fake@0", evidence=rows,
        )

    def link_dataset(self, ds: Dataset) -> Dataset:
        self.calls += 1
        caps = [(inc, c.id) for inc in ds.incidents for c in inc.captures]
        links = []
        for (ia, a), (ib, b) in combinations(caps, 2):
            template = {Layer.kit, Layer.platform} if self.link_platform else {Layer.kit}
            for relation, layers in ((Relation.campaign, self._campaign_layers()), (Relation.template, template)):
                l = self._link(len(links), ia, a, ib, b, layers, relation)
                if l:
                    links.append(l)
        # connected components over campaign links
        parent = {c: c for _, c in caps}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        for l in links:
            if l.relation is Relation.campaign:
                parent[find(l.a)] = find(l.b)
        comps: dict[str, list[str]] = {}
        for _, c in caps:
            comps.setdefault(find(c), []).append(c)
        campaigns = [
            Campaign(id=f"camp{n}", label=f"c{n}", relation=Relation.campaign, capture_ids=sorted(m),
                     link_ids=[l.id for l in links if l.relation is Relation.campaign and l.a in m])
            for n, m in enumerate(sorted((m for m in comps.values() if len(m) > 1), key=min))
        ]
        return Dataset(name=ds.name, incidents=ds.incidents, links=links, campaigns=campaigns)

    def link_incident(self, base: Dataset, incident: Incident, relation: Relation = Relation.campaign) -> Result:
        layers = self._campaign_layers()
        inc_of = {c.id: i for i in base.incidents for c in i.captures}
        links = []
        for c in incident.captures:
            for other, oi in inc_of.items():
                l = self._link(len(links), incident, c.id, oi, other, layers, Relation.campaign)
                if l:
                    links.append(l)
        candidates = []
        for camp in base.campaigns:
            bits = sum(l.score_bits for l in links if l.b in camp.capture_ids)
            if bits > 0:
                candidates.append(Candidate(campaign_id=camp.id, score_bits=bits))
        capacity = BITS * sum(1 for i in incident.indicators if i.layer in IDENTITY and i.value is not None)
        best = max(candidates, key=lambda c: c.score_bits, default=None)
        if best and best.score_bits >= self.threshold:
            a = Assignment(incident_id=incident.id, candidates=candidates, decision=Decision.assign,
                           campaign_id=best.campaign_id, evidence_capacity_bits=capacity, method="fake@0")
        else:
            a = Assignment(incident_id=incident.id, candidates=candidates,
                           decision=Decision.review if candidates else Decision.new,
                           evidence_capacity_bits=capacity, method="fake@0")
        return Result(assignment=a, links=links)
