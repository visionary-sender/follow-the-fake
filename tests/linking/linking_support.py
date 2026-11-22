"""Synthetic datasets and a stub scorer for the grouping / assignment tests (no real data)."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from ftf.contract.models import (
    Artefact,
    ArtefactType,
    Capture,
    Dataset,
    EvidenceRow,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Level,
    MissingModality,
    Observed,
    Provenance,
    Relation,
    Stage,
)
from ftf.linking.backend import Backend
from ftf.linking.types import PairScore

TAU = 6.0
CONFLICT = -4.0
WEIGHTS = {
    Layer.tenant: 5.0,
    Layer.owner: 5.0,
    Layer.tracker_instance: 5.0,
    Layer.landing: 4.0,
    Layer.tracking_account: 4.0,
    Layer.kit: 3.0,
    Layer.creative: 3.0,
    Layer.offer: 2.0,
    Layer.distribution_account: 2.0,
    Layer.platform: 1.5,
    Layer.infrastructure: 1.0,
}
RELATION_LAYERS = {
    Relation.template: {Layer.kit, Layer.landing, Layer.creative, Layer.offer},
    Relation.tracking_account: {Layer.tracking_account, Layer.tracker_instance},
    Relation.operator: set(WEIGHTS),
    Relation.campaign: set(WEIGHTS),
    Relation.ecosystem: {Layer.platform, Layer.creative, Layer.offer, Layer.kit},
}
SINGLE = {Layer.platform, Layer.tenant, Layer.owner}
PRIVATE = {Layer.tenant, Layer.owner, Layer.tracker_instance, Layer.tracking_account}

# Which artefact a layer's indicator is extracted from in the synthetic data.
SOURCE = {
    Layer.creative: ArtefactType.ad_screenshot,
    Layer.tracking_account: ArtefactType.har,
    Layer.tracker_instance: ArtefactType.har,
    Layer.infrastructure: ArtefactType.har,
}


def cap(cid: str, stage: str = "lander", arts: Iterable[str] = ("html", "har", "ad_screenshot"), missing: Iterable[tuple[str, str]] = (), duplicate_of: Optional[str] = None) -> Capture:
    return Capture(
        id=cid,
        stage=Stage(stage),
        artefacts=[Artefact(id=f"{cid}/{t}", type=ArtefactType(t)) for t in arts],
        missing=[MissingModality(modality=ArtefactType(m), reason=r) for m, r in missing],
        duplicate_of=duplicate_of,
    )


def ind(cid: str, layer: Layer, value: str, n: int, live: bool = False, captures: Optional[dict[str, Capture]] = None) -> Indicator:
    art = SOURCE.get(layer, ArtefactType.html)
    has = captures is None or any(a.type is art for a in captures[cid].artefacts)
    return Indicator(
        id=f"{cid}:i{n}",
        capture_id=cid,
        layer=layer,
        kind=f"{layer.value}_id",
        level=Level.campaign,
        value=value,
        observed=Observed.live if live else Observed.static,
        provenance=Provenance(artefact_id=f"{cid}/{art.value}" if has else None),
        extractor="synthetic@0",
    )


def incident(iid: str, captures: list[Capture], values: dict[str, list[tuple]]) -> Incident:
    """values: capture id -> [(layer, value) or (layer, value, live)]."""
    by_id = {c.id: c for c in captures}
    inds = []
    n = 0
    for cid, vals in values.items():
        for v in vals:
            n += 1
            inds.append(ind(cid, v[0], v[1], n, live=len(v) > 2 and v[2], captures=by_id))
    return Incident(id=iid, source="synthetic", captures=captures, indicators=inds)


def simple(iid: str, *vals: tuple, stage: str = "lander", arts: Iterable[str] = ("html", "har", "ad_screenshot"), missing=()) -> Incident:
    cid = f"{iid}-c1"
    return incident(iid, [cap(cid, stage, arts, missing)], {cid: list(vals)})


def dataset(*incidents: Incident, name: str = "synthetic") -> Dataset:
    return Dataset(name=name, incidents=list(incidents))


class StubScorer:
    """Hand-table stand-in for the HACK-21 ledger: fixed bits per shared layer, -4 per single-valued conflict."""

    def __init__(self, ds: Dataset):
        canon = {c.id: (c.duplicate_of or c.id) for i in ds.incidents for c in i.captures}
        self.values: dict[str, dict[Layer, dict[str, list]]] = defaultdict(lambda: defaultdict(dict))
        for i in ds.indicators():
            if i.layer is Layer.collector or i.value is None:
                continue
            self.values[canon[i.capture_id]][i.layer].setdefault(i.value, []).append(i)
        self.calls = 0

    def __call__(self, a: str, b: str, relation: Relation) -> PairScore:
        self.calls += 1
        rows, bits, positive, live = [], 0.0, 0, False
        for layer in sorted(RELATION_LAYERS[relation], key=lambda l: l.value):
            va, vb = self.values[a].get(layer, {}), self.values[b].get(layer, {})
            shared = sorted(set(va) & set(vb))
            if shared:
                w = WEIGHTS[layer]
                ids = [x.id for v in shared for x in va[v] + vb[v]]
                rows.append(EvidenceRow(layer=layer, kind=f"{layer.value}_id", status=EvidenceStatus.shared, value=shared[0], bits=w, indicator_ids=ids))
                bits += w
                positive += 1
                obs = [x.observed for v in shared for x in va[v] + vb[v]]
                if layer in PRIVATE and all(o is Observed.live for o in obs):
                    live = True
            elif va and vb and layer in SINGLE and relation in (Relation.operator, Relation.campaign):
                rows.append(EvidenceRow(layer=layer, kind=f"{layer.value}_id", status=EvidenceStatus.conflict, bits=CONFLICT))
                bits += CONFLICT
        return PairScore(a=a, b=b, relation=relation, bits=bits, rows=rows, layers_positive=positive, live_private=live)


def stub_backend(ds: Dataset) -> Backend:
    scorer = StubScorer(ds)
    return Backend(
        dataset=ds,
        score=scorer,
        threshold=lambda relation: TAU,
        layer_caps=lambda relation: {l: WEIGHTS[l] for l in RELATION_LAYERS[relation]},
    )
