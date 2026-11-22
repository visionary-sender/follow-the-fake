"""Candidate index: find the campaigns a new incident can match without rescanning the collection.

Method (07 §3 D2 "Data model" and "Assignment (open set)"):

* Built once from a linked base (the output of `ftf link`): an inverted index
  (layer, kind, normalised value) -> canonical captures, over informative values only (collector
  kinds, ignored values and kinds whose prior is 0 under every relation are left out); capture ->
  campaign per relation; and per-value rarity counts (incidents and pre-groups carrying it).
* A new capture can only earn positive bits against a campaign member it shares a value with
  (the ledger has no other source of positive bits). So the campaigns reachable through the
  incident's informative values are a superset of the campaigns that can clear any bar, and
  `assign` / `link_incident` score only those (plus a few zero-bit campaigns needed to fill the
  candidate list exactly as the linear scan would; see `assign._select`).
* The scoring context of "base plus one incident" is the base context extended by the incident
  (`extend_context`): copy-on-write overlays, so its cost is the incident's size, not the
  collection's (pre-groups are recomputed only when the incident carries a live private or owner id).
* Vector views (HACK-24): `add_vector_view(view, capture_ids, matrix)` stores a matrix; `nearest`
  is an exact top-k by dot product. Enough at our scale, no new dependency.

Persisted as JSON (plus one `.npy` per vector view). No pickle. The scoring context itself is not
stored: `load(path, base)` rebuilds it from the base dataset (one pass), and without a base the
loaded index still answers `candidates()`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import ChainMap, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.contract.models import (
    COLLECTOR_KINDS,
    ArtefactType,
    Dataset,
    Incident,
    Layer,
    Observed,
    Relation,
    Stage,
    linkable,
)
from ftf.linking.ledger import ScoringContext, _pre_groups, _Values, normalise
from ftf.linking.weights import HAND_TABLE, IGNORED_VALUES, WeightTable

INDEX_VERSION = 1
Key = tuple[Layer, str, str]  # (layer, kind, normalised value)

_CACHE: dict[int, tuple[Dataset, "CandidateIndex"]] = {}
_CACHE_SIZE = 4


@dataclass(frozen=True)
class CampaignRef:
    """The two fields of a contract `Campaign` that assignment reads."""

    id: str
    capture_ids: tuple[str, ...]


@dataclass
class VectorView:
    capture_ids: list[str]
    matrix: np.ndarray  # (n, dim), one row per capture id

    def nearest(self, vector: np.ndarray, k: int) -> list[tuple[str, float]]:
        """Exact top-k rows by dot product (cosine if the rows and the query are unit-norm)."""
        if not self.capture_ids or k <= 0:
            return []
        sims = self.matrix @ np.asarray(vector, dtype=self.matrix.dtype)
        k = min(k, len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = sorted(top, key=lambda i: (-float(sims[i]), self.capture_ids[i]))
        return [(self.capture_ids[i], float(sims[i])) for i in top]


def dataset_digest(ds: Dataset) -> str:
    """Identity of a linked base: everything the saved index is built from.

    Incidents, captures (stage, duplicate_of), indicators (capture, layer, kind, value, observed,
    artefact) and campaigns. Re-enriching the base or re-flagging a decoy changes it.
    """
    h = hashlib.sha256()
    for inc in ds.incidents:
        h.update(inc.id.encode() + b"\x01")
        for c in inc.captures:
            h.update("\x00".join((c.id, c.stage.value, c.duplicate_of or "", ",".join(sorted(a.id + "=" + a.type.value for a in c.artefacts)))).encode() + b"\x02")
        for i in inc.indicators:
            h.update("\x00".join((i.id, i.capture_id, i.layer.value, i.kind, i.value or "", i.observed.value, i.provenance.artefact_id or "")).encode() + b"\x03")
    for camp in ds.campaigns:
        h.update(f"{camp.relation.value}\x00{camp.id}\x00{','.join(camp.capture_ids)}\x04".encode())
    return h.hexdigest()


def _prior_cap(weights: WeightTable, kind: str, layer: Layer, relation: Relation) -> float:
    """Most bits one shared value of this kind can earn under `relation` (live, before rarity)."""
    return weights.weight(kind, layer).bits * weights.scale(kind, layer, relation)


def _informative_kind(weights: WeightTable, kind: str, layer: Layer) -> bool:
    if layer is Layer.collector or kind in COLLECTOR_KINDS:
        return False
    return any(_prior_cap(weights, kind, layer, r) > 0 for r in Relation)


def modalities_of(incidents: Iterable[Incident]) -> dict[Layer, set[ArtefactType]]:
    """Which artefact types each layer's indicators were extracted from (see `assign.layer_modalities`)."""
    incidents = list(incidents)
    art_type = {a.id: a.type for i in incidents for c in i.captures for a in c.artefacts}
    out: dict[Layer, set[ArtefactType]] = defaultdict(set)
    for inc in incidents:
        for ind in inc.indicators:
            if not linkable(ind):
                continue
            t = art_type.get(ind.provenance.artefact_id or "")
            if t is not None:
                out[ind.layer].add(t)
    return out


@dataclass
class _Extras:
    """Base-context inputs of `_pre_groups` that `ScoringContext` does not keep."""

    live_private: dict[Key, set[str]] = field(default_factory=lambda: defaultdict(set))
    owners: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    incidents: set[str] = field(default_factory=set)  # incidents with at least one canonical capture


def _extras(ds: Dataset, ctx: ScoringContext) -> _Extras:
    """The same live-private and owner maps `ScoringContext.from_dataset` builds, in the same way."""
    ex = _Extras()
    for inc in ds.incidents:
        for ind in inc.indicators:
            if ind.value is None:
                continue
            norm = normalise(ind.value, ind.layer)
            if not norm or norm.casefold() in IGNORED_VALUES:
                continue
            who = ctx.incident_of[ctx.canonical[ind.capture_id]]
            if ind.layer is Layer.owner:
                ex.owners[who].add(f"{ind.kind}={norm}")
            if ind.observed is Observed.live and ctx.weights.weight(ind.kind, ind.layer).private:
                ex.live_private[(ind.layer, ind.kind, norm)].add(who)
    ex.incidents = {ctx.incident_of[c] for c in ctx.values}
    return ex


def extend_context(ctx: ScoringContext, extras: _Extras, incident: Incident) -> tuple[ScoringContext, _Extras]:
    """`ScoringContext.from_dataset(base + incident)` built from the base context, without a rescan.

    Mirrors `from_dataset` step by step (the incident's indicators come after the base's, as in
    `assign.combine`). Dicts are copy-on-write overlays (`ChainMap`) over the base context, so
    the cost is the incident's size; pre-groups are recomputed only if the incident adds a live
    private id or an owner id. `ds` stays the base dataset (scoring never reads it).
    """
    canonical: dict[str, str] = {}
    incident_of: dict[str, str] = {}
    for cap in incident.captures:
        canonical[cap.id] = cap.duplicate_of or cap.id
        incident_of[cap.id] = incident.id
    canonical_all = ChainMap(canonical, ctx.canonical)
    incident_of_all = ChainMap(incident_of, ctx.incident_of)

    values: dict[str, dict] = {}
    layers: dict[str, set[Layer]] = {}
    for c in set(canonical.values()):
        if c in ctx.values:
            continue  # a base capture: copied below only if the incident adds to it
        values[c], layers[c] = {}, set()

    def own_values(cap: str) -> dict:
        if cap not in values:
            values[cap] = {
                k: _Values(ids=defaultdict(list, {n: list(v) for n, v in x.ids.items()}), display=dict(x.display), live=set(x.live))
                for k, x in ctx.values[cap].items()
            }
            layers[cap] = set(ctx.layers[cap])
        return values[cap]

    index: dict[Key, set[str]] = {}
    live_private: dict[Key, set[str]] = {}
    owners: dict[str, set[str]] = {}
    for ind in incident.indicators:
        cap = canonical_all[ind.capture_id]
        vals_of_cap = own_values(cap)
        layers[cap].add(ind.layer)
        if ind.value is None:
            continue
        norm = normalise(ind.value, ind.layer)
        if not norm or norm.casefold() in IGNORED_VALUES:
            continue
        vals = vals_of_cap.setdefault((ind.layer, ind.kind), _Values())
        vals.ids[norm].append(ind.id)
        vals.display.setdefault(norm, ind.value)
        if ind.observed is Observed.live:
            vals.live.add(norm)
        key = (ind.layer, ind.kind, norm)
        who = incident_of_all[cap]
        if key not in index:
            index[key] = set(ctx.index.get(key, ()))
        index[key].add(who)
        if ind.layer is Layer.owner:
            if who not in owners:
                owners[who] = set(extras.owners.get(who, ()))
            owners[who].add(f"{ind.kind}={norm}")
        if ind.observed is Observed.live and ctx.weights.weight(ind.kind, ind.layer).private:
            if key not in live_private:
                live_private[key] = set(extras.live_private.get(key, ()))
            live_private[key].add(who)

    new_caps = [c for c in values if c not in ctx.values]
    inc_all = extras.incidents | {incident_of_all[c] for c in new_caps}
    lp_all = ChainMap(live_private, extras.live_private)
    owners_all = ChainMap(owners, extras.owners)
    if live_private or owners:
        group_of = _pre_groups(inc_all, dict(lp_all), dict(owners_all))
    else:
        # Nothing new can join two components: the base groups stand, new incidents are singletons.
        group_of = ChainMap({i: i for i in inc_all - extras.incidents}, ctx.group_of)
    n_groups = len(set(group_of.values())) if live_private or owners else ctx.n_groups + len(inc_all - extras.incidents)
    ext = replace(
        ctx,
        canonical=canonical_all,
        incident_of=incident_of_all,
        indicator_incident=ChainMap({ind.id: incident.id for ind in incident.indicators}, ctx.indicator_incident),
        values=ChainMap(values, ctx.values),
        layers=ChainMap(layers, ctx.layers),
        index=ChainMap(index, ctx.index),
        group_of=group_of,
        n_groups=n_groups,
        n=ctx.n + len(new_caps),
        n_incidents=len(inc_all),
    )
    return ext, _Extras(live_private=lp_all, owners=owners_all, incidents=inc_all)


@dataclass
class CandidateIndex:
    digest: str
    postings: dict[Key, set[str]]  # informative value -> canonical captures carrying it
    counts: dict[Key, tuple[int, int]]  # value -> (incidents, pre-groups) carrying it in the base
    campaigns: dict[Relation, list[CampaignRef]]  # per relation, sorted by id
    campaign_of: dict[Relation, dict[str, list[str]]]  # canonical capture -> campaign ids
    stage: dict[str, Stage]  # capture id -> stage
    canonical: dict[str, str]  # capture id -> canonical capture id
    modalities: dict[Layer, set[ArtefactType]]
    incidents: set[str]
    n_captures: int
    vectors: dict[str, VectorView] = field(default_factory=dict)
    weights: WeightTable = HAND_TABLE
    _ctx: Optional[ScoringContext] = field(default=None, repr=False)
    _extras: Optional[_Extras] = field(default=None, repr=False)
    _base: Optional[Dataset] = field(default=None, repr=False)
    _added: list[Incident] = field(default_factory=list, repr=False)  # incidents added since `attach`
    _artefacts: set[str] = field(default_factory=set, repr=False)  # artefact ids of base + added
    _shape: tuple = field(default=(), repr=False)  # cheap fingerprint of the cached base object

    # --- build / persist --------------------------------------------------------------------

    @classmethod
    def build(cls, base: Dataset, weights: WeightTable = HAND_TABLE, cache: bool = True) -> "CandidateIndex":
        """Index a linked base. One pass over its indicators plus one scoring-context build."""
        idx = cls._skeleton(base, weights)
        idx.attach(base)
        ctx = idx._ctx
        assert ctx is not None
        for key in list(idx.postings):
            idx.counts[key] = (len(ctx.index.get(key, ())), ctx.groups(key))
        if cache:
            _remember(base, idx)
        return idx

    @classmethod
    def _skeleton(cls, base: Dataset, weights: WeightTable) -> "CandidateIndex":
        canonical = {c.id: (c.duplicate_of or c.id) for i in base.incidents for c in i.captures}
        postings: dict[Key, set[str]] = defaultdict(set)
        for ind in base.indicators():
            if ind.value is None or not _informative_kind(weights, ind.kind, ind.layer):
                continue
            norm = normalise(ind.value, ind.layer)
            if not norm or norm.casefold() in IGNORED_VALUES:
                continue
            postings[(ind.layer, ind.kind, norm)].add(canonical[ind.capture_id])
        campaigns: dict[Relation, list[CampaignRef]] = {r: [] for r in Relation}
        campaign_of: dict[Relation, dict[str, list[str]]] = {r: defaultdict(list) for r in Relation}
        for camp in sorted(base.campaigns, key=lambda c: c.id):
            campaigns[camp.relation].append(CampaignRef(camp.id, tuple(camp.capture_ids)))
            for m in dict.fromkeys(canonical.get(x, x) for x in camp.capture_ids):
                campaign_of[camp.relation][m].append(camp.id)
        return cls(
            digest=dataset_digest(base),
            postings=dict(postings),
            counts={},
            campaigns=campaigns,
            campaign_of={r: dict(m) for r, m in campaign_of.items()},
            stage={c.id: c.stage for i in base.incidents for c in i.captures},
            canonical=canonical,
            modalities=modalities_of(base.incidents),
            incidents={i.id for i in base.incidents},
            n_captures=len(set(canonical.values())),
            weights=weights,
        )

    def attach(self, base: Dataset) -> None:
        """Build the scoring context of `base` (needed for exact assignment). Checks the digest."""
        if dataset_digest(base) != self.digest:
            raise ValueError("the index was built from another base (digest differs); rebuild it with `ftf index`")
        ctx = ScoringContext.from_dataset(base, self.weights)
        self._ctx, self._extras = ctx, _extras(base, ctx)
        self._base = base
        self._added = []
        self._artefacts = {a.id for i in base.incidents for c in i.captures for a in c.artefacts}

    @property
    def ready(self) -> bool:
        return self._ctx is not None

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        views = {}
        for view, vv in sorted(self.vectors.items()):
            npy = path.with_name(f"{path.stem}.{_safe(view)}-{hashlib.sha256(view.encode()).hexdigest()[:8]}.npy")
            np.save(npy, vv.matrix, allow_pickle=False)
            views[view] = {"file": npy.name, "capture_ids": vv.capture_ids}
        data = {
            "version": INDEX_VERSION,
            "digest": self.digest,
            "weights": weights_fingerprint(self.weights),
            "n_captures": self.n_captures,
            "incidents": sorted(self.incidents),
            "postings": [[k[0].value, k[1], k[2], sorted(v), *self._count(k)] for k, v in sorted(self.postings.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2]))],
            "campaigns": {r.value: [[c.id, list(c.capture_ids)] for c in cs] for r, cs in self.campaigns.items()},
            "stage": {k: v.value for k, v in sorted(self.stage.items())},
            "canonical": {k: v for k, v in sorted(self.canonical.items()) if k != v},
            "modalities": {l.value: sorted(t.value for t in ts) for l, ts in sorted(self.modalities.items(), key=lambda kv: kv[0].value)},
            "vectors": views,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, base: Optional[Dataset] = None, weights: WeightTable = HAND_TABLE) -> "CandidateIndex":
        """Read an index written by `save`. With `base`, the scoring context is rebuilt (digest-checked)."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != INDEX_VERSION:
            raise ValueError(f"index version {data.get('version')} != {INDEX_VERSION}; rebuild it")
        if data.get("weights") != weights_fingerprint(weights):
            raise ValueError("the index was built with another weight table; rebuild it with `ftf index`")
        postings, counts = {}, {}
        for layer, kind, value, caps, n_inc, n_grp in data["postings"]:
            key = (Layer(layer), kind, value)
            postings[key] = set(caps)
            counts[key] = (n_inc, n_grp)
        campaigns = {r: [] for r in Relation}
        campaign_of: dict[Relation, dict[str, list[str]]] = {r: defaultdict(list) for r in Relation}
        stage = {k: Stage(v) for k, v in data["stage"].items()}
        canonical = {k: k for k in stage} | data["canonical"]
        for r, cs in data["campaigns"].items():
            rel = Relation(r)
            for cid, caps in cs:
                campaigns[rel].append(CampaignRef(cid, tuple(caps)))
                for m in dict.fromkeys(canonical.get(x, x) for x in caps):
                    campaign_of[rel][m].append(cid)
        vectors = {}
        for view, v in data.get("vectors", {}).items():
            npy = path.with_name(Path(v["file"]).name)
            vectors[view] = VectorView(capture_ids=list(v["capture_ids"]), matrix=np.load(npy, allow_pickle=False))
        idx = cls(
            digest=data["digest"],
            postings=postings,
            counts=counts,
            campaigns=campaigns,
            campaign_of={r: dict(m) for r, m in campaign_of.items()},
            stage=stage,
            canonical=canonical,
            modalities={Layer(l): {ArtefactType(t) for t in ts} for l, ts in data["modalities"].items()},
            incidents=set(data["incidents"]),
            n_captures=data["n_captures"],
            vectors=vectors,
            weights=weights,
        )
        if base is not None:
            idx.attach(base)
            _remember(base, idx)
        return idx

    # --- vectors (HACK-24 hook) -------------------------------------------------------------

    def add_vector_view(self, view: str, capture_ids: list[str], matrix: np.ndarray) -> None:
        matrix = np.asarray(matrix)
        if matrix.ndim != 2 or matrix.shape[0] != len(capture_ids):
            raise ValueError(f"vector view {view}: matrix shape {matrix.shape} does not match {len(capture_ids)} captures")
        self.vectors[view] = VectorView(capture_ids=list(capture_ids), matrix=matrix)

    def nearest(self, view: str, vector: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        vv = self.vectors.get(view)
        return vv.nearest(vector, k) if vv is not None else []

    # --- queries ----------------------------------------------------------------------------

    def probe_keys(self, incident: Incident) -> set[Key]:
        return self._keys(incident.indicators)

    def _keys(self, indicators: Iterable) -> set[Key]:
        out = set()
        for ind in indicators:
            if ind.value is None or not _informative_kind(self.weights, ind.kind, ind.layer):
                continue
            norm = normalise(ind.value, ind.layer)
            if norm and norm.casefold() not in IGNORED_VALUES:
                out.add((ind.layer, ind.kind, norm))
        return out

    def reachable(
        self, keys: Iterable[Key], relation: Relation, informative=None, captures: Iterable[str] = ()
    ) -> set[str]:
        """Campaign ids (of `relation`) with a member carrying one of `keys` or listed in `captures`.

        `informative(key)` decides which keys may propose campaigns (default: the prior under
        `relation` minus the base rarity reaches the hub floor).
        """
        if informative is None:
            informative = lambda key: self._upper_bits(key, relation) >= self.weights.hub_bits  # noqa: E731
        of = self.campaign_of.get(relation, {})
        caps: set[str] = set(captures)
        for key in keys:
            if key in self.postings and informative(key):
                caps |= self.postings[key]
        return {cid for c in caps for cid in of.get(self.canonical.get(c, c), ())}

    def _upper_bits(self, key: Key, relation: Relation) -> float:
        prior = _prior_cap(self.weights, key[1], key[0], relation)
        return prior - math.log2(max(self._count(key)[1], 1))

    def _count(self, key: Key) -> tuple[int, int]:
        """(incidents, pre-groups) carrying `key`: live from the scoring context when attached
        (so `add()` never has to refresh them), else as saved."""
        if self._ctx is not None:
            return len(self._ctx.index.get(key, ())), self._ctx.groups(key)
        return self.counts.get(key, (0, 0))

    def candidates(
        self,
        incident: Incident,
        relation: Relation = Relation.campaign,
        k: Optional[int] = 10,
        vectors: Optional[dict[str, np.ndarray]] = None,
        neighbours: int = 10,
    ) -> list[str]:
        """Campaign ids worth scoring for `incident`, best first (works from a loaded index alone).

        Reachable campaigns are ranked by an upper bound: per layer, the best `prior - log2(groups)`
        over the shared values (base rarity). Campaigns holding a nearest neighbour of one of the
        incident's vectors (`vectors`: view -> query vector) are added after them.
        This is the cheap pre-filter; `assign` scores every reachable campaign exactly.
        """
        relation = Relation(relation)
        per: dict[str, dict[Layer, float]] = defaultdict(dict)
        of = self.campaign_of.get(relation, {})
        for key in self.probe_keys(incident):
            if key not in self.postings:
                continue
            bits = self._upper_bits(key, relation)
            if bits < self.weights.hub_bits:
                continue
            for c in self.postings[key]:
                for cid in of.get(c, ()):
                    per[cid][key[0]] = max(per[cid].get(key[0], 0.0), bits)
        ranked = sorted(per, key=lambda cid: (-sum(per[cid].values()), cid))
        seen = set(ranked)
        for view, vec in sorted((vectors or {}).items()):
            for cap, _sim in self.nearest(view, vec, neighbours):
                for cid in of.get(self.canonical.get(cap, cap), ()):
                    if cid not in seen:
                        seen.add(cid)
                        ranked.append(cid)
        return ranked if k is None else ranked[:k]

    # --- assignment support -----------------------------------------------------------------

    def _check_new(self, incident: Incident) -> None:
        if self._ctx is None or self._extras is None:
            raise ValueError("the index has no scoring context; load it with the base dataset (or call attach)")
        if incident.id in self.incidents:
            raise ValueError(f"incident {incident.id} is already in the collection")
        # The linear path rejects these when it validates base + incident; the index must too.
        reused = sorted({c.id for c in incident.captures if c.id in self._ctx.canonical})
        reused += sorted({i.id for i in incident.indicators if i.id in self._ctx.indicator_incident})
        reused += sorted({a.id for c in incident.captures for a in c.artefacts if a.id in self._artefacts})
        if reused:
            raise ValueError(f"incident {incident.id} reuses ids already in the collection: {', '.join(reused[:5])}")
        own = {c.id for c in incident.captures}
        for c in incident.captures:
            t = c.duplicate_of
            if t is not None and t not in own and self._ctx.canonical.get(t) != t:
                raise ValueError(f"capture {c.id}: duplicate_of {t} is not a canonical capture of the collection")

    def extend(self, incident: Incident) -> ScoringContext:
        """Scoring context of the base plus `incident` (the index itself is not changed).

        The result shares the index's dicts underneath: use it before the next `add()`.
        """
        self._check_new(incident)
        assert self._ctx is not None and self._extras is not None
        return extend_context(self._ctx, self._extras, incident)[0]

    def add(self, incident: Incident) -> None:
        """Add an assigned incident to the base (rarity, τ, duplicates); campaigns stay as they are.

        This is what `ftf assign` does between incidents. The overlays of `extend_context` are
        written into the index's own context in place, so lookups stay one level deep however
        many incidents are added. The index then no longer matches the original base file, so it
        leaves the cache.
        """
        self._check_new(incident)
        ctx, ex = self._ctx, self._extras
        assert ctx is not None and ex is not None
        ext, ext_ex = extend_context(ctx, ex, incident)
        flat = {}
        for name in ("canonical", "incident_of", "indicator_incident", "values", "layers", "index", "group_of"):
            new, old = getattr(ext, name), getattr(ctx, name)
            if isinstance(new, ChainMap) and new.maps[1] is old:
                old.update(new.maps[0])  # the overlay holds copies, so writing it in place is safe
                flat[name] = old
            else:
                flat[name] = dict(new)  # recomputed (pre-groups)
        self._ctx = replace(ext, **flat)
        for name in ("live_private", "owners"):
            new, old = getattr(ext_ex, name), getattr(ex, name)
            old.update(new.maps[0])
        ex.incidents = ext_ex.incidents
        self._added.append(incident)
        self._artefacts |= {a.id for c in incident.captures for a in c.artefacts}
        for cap in incident.captures:
            self.canonical[cap.id] = cap.duplicate_of or cap.id
            self.stage[cap.id] = cap.stage
        # The incident's own captures are in no campaign, so they propose none. But a capture that
        # duplicates a campaign member merges its values into that member: those values must
        # reach the member's campaigns from now on.
        for ind in incident.indicators:
            canon = self.canonical[ind.capture_id]
            if canon != ind.capture_id and any(canon in m for m in self.campaign_of.values()):
                for key in self._keys([ind]):
                    self.postings.setdefault(key, set()).add(canon)
        for layer, mods in modalities_of([incident]).items():
            self.modalities.setdefault(layer, set()).update(mods)
        self.incidents.add(incident.id)
        self.n_captures = self._ctx.n
        self.digest = ""
        for k in [k for k, (_, idx) in _CACHE.items() if idx is self]:
            del _CACHE[k]


def weights_fingerprint(weights: WeightTable) -> str:
    """Stable across processes (no set or hash order): kinds, defaults, scales and constants."""
    def rel(m) -> list:
        return sorted((r.value, float(v)) for r, v in (m or {}).items())

    canon = {
        "kinds": sorted(
            (k, e.layer.value, float(e.bits), e.single_valued, e.private, rel(e.relations) if e.relations is not None else None)
            for k, e in weights.kinds.items()
        ),
        "defaults": sorted((l.value, float(b)) for l, b in weights.layer_defaults.items()),
        "scale": sorted((l.value, rel(m)) for l, m in weights.relation_scale.items()),
        "conflict_relations": sorted(r.value for r in weights.conflict_relations),
        "constants": [weights.conflict_bits, weights.hub_bits, weights.static_discount],
    }
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()[:16]


def _safe(view: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in view) or "view"


def _shape(ds: Dataset) -> tuple:
    return (len(ds.incidents), len(ds.campaigns), len(ds.links), len(ds.assignments), sum(len(i.indicators) for i in ds.incidents))


def _remember(base: Dataset, idx: CandidateIndex) -> None:
    idx._shape = _shape(base)
    _CACHE[id(base)] = (base, idx)
    while len(_CACHE) > _CACHE_SIZE:
        del _CACHE[next(iter(_CACHE))]


def cached_index(base: Dataset) -> Optional[CandidateIndex]:
    """The index built (or loaded with a base) for this very `Dataset` object, if any.

    A cheap shape check drops it if the object was edited in place (incidents, campaigns, links,
    assignments or indicators added or removed); edit values in place and you must rebuild.
    """
    hit = _CACHE.get(id(base))
    if hit is None or hit[0] is not base or not hit[1].ready:
        return None
    if hit[1]._shape != _shape(base):  # edited in place since indexing
        del _CACHE[id(base)]
        return None
    return hit[1]


def clear_cache() -> None:
    _CACHE.clear()
