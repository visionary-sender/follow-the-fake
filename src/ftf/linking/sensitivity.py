"""Fragile links and decisions: does a verdict survive moving the weight table by ±2 bits?

Every prior in the hand table is a judgement call. For each link (and each assignment) the
ledger is recomputed with one layer's weights moved by −delta and by +delta (every kind of the
layer and the layer default, floored at 0 bits), and once more with every layer moved together
(the worst case: all down for a link, all up for a non-link). τ, rarity and pre-groups do not
change. A link or decision whose verdict changes under a single-layer move is *fragile*; one
that changes only when every layer moves at once is reported as flipping in the worst case.

Results go into existing contract fields only: `EvidenceRow.note` on the link's ledger rows,
`Assignment.note` and `Candidate.note`, e.g. "fragile: flips to review if tracking_account −2 bits".

The link verdict here is the grouping edge rule (`grouping.qualifies` with `MIN_LAYERS`, and no
tenant/owner conflict under operator / campaign): `link`; otherwise `review` within
`REVIEW_BAND` below τ (and above 0 bits); otherwise `no link`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Optional

from ftf.contract.models import Assignment, Dataset, EvidenceStatus, Incident, Layer, Link, Relation
from ftf.linking.grouping import CANNOT_LINK_RELATIONS, MIN_LAYERS, has_conflict, qualifies
from ftf.linking.ledger import REVIEW_BAND, ScoringContext, score_pair, threshold
from ftf.linking.types import PairScore
from ftf.linking.weights import HAND_TABLE, WeightTable

DELTA = 2.0
WORST = "all layers"
SCORING_LAYERS: tuple[Layer, ...] = tuple(l for l in Layer if l is not Layer.collector)


def perturb(weights: WeightTable, layers: tuple[Layer, ...], delta: float) -> WeightTable:
    """The table with every kind of `layers` (and those layers' defaults) moved by `delta` bits, floored at 0."""
    kinds = {
        k: replace(e, bits=max(e.bits + delta, 0.0)) if e.layer in layers and e.bits > 0 else e
        for k, e in weights.kinds.items()
    }
    defaults = {
        l: max(b + delta, 0.0) if l in layers and b > 0 else b for l, b in weights.layer_defaults.items()
    }
    return replace(weights, kinds=kinds, layer_defaults=defaults)


def _fmt(delta: float) -> str:
    return f"{'+' if delta > 0 else '−'}{abs(delta):g} bits"


def link_state(ps: PairScore, tau: float) -> str:
    rel = ps.relation
    if qualifies(ps, tau, MIN_LAYERS[rel]) and not (rel in CANNOT_LINK_RELATIONS and has_conflict(ps)):
        return "link"
    if ps.bits > 0 and ps.bits >= tau - REVIEW_BAND:
        return "review"
    return "no link"


class _Tables:
    """Perturbed weight tables and contexts, built once per (layers, delta)."""

    def __init__(self, ctx: ScoringContext, delta: float):
        self.ctx, self.delta = ctx, delta
        self._ctx: dict[tuple[tuple[Layer, ...], float], ScoringContext] = {}

    def context(self, layers: tuple[Layer, ...], delta: float) -> ScoringContext:
        key = (layers, delta)
        if key not in self._ctx:
            self._ctx[key] = replace(self.ctx, weights=perturb(self.ctx.weights, layers, delta))
        return self._ctx[key]


def link_flips(ctx: ScoringContext, a: str, b: str, relation: Relation, delta: float = DELTA, tables: Optional[_Tables] = None) -> dict:
    """The verdict of one capture pair and how it changes under each ±delta move."""
    tables = tables or _Tables(ctx, delta)
    tau = threshold(ctx, relation)
    ps = score_pair(ctx, a, b, relation)
    state = link_state(ps, tau)
    # A layer can only move the score through a value both captures carry (shared or hub-subsumed rows).
    layers = sorted(
        {r.layer for r in ps.rows if r.status in (EvidenceStatus.shared, EvidenceStatus.hub_subsumed) and r.layer is not Layer.collector},
        key=lambda l: list(Layer).index(l),
    )
    flips = []
    for layer in layers:
        for d in (-delta, delta):
            s = link_state(score_pair(tables.context((layer,), d), a, b, relation), tau)
            if s != state:
                flips.append({"layer": layer.value, "delta": d, "to": s})
    # Worst case: every layer at once, against the verdict (down for a link, up for no link,
    # both ways for review).
    worst_d, worst = 0.0, state
    for d in {"link": (-delta,), "no link": (delta,)}.get(state, (-delta, delta)):
        s = link_state(score_pair(tables.context(SCORING_LAYERS, d), a, b, relation), tau)
        if s != state:
            worst_d, worst = d, s
            break
    return {
        "a": ps.a,
        "b": ps.b,
        "relation": relation.value,
        "bits": ps.bits,
        "tau": tau,
        "state": state,
        "flips": flips,
        "worst_case": None if worst == state else {"delta": worst_d, "to": worst},
        "fragile": bool(flips),
    }


def fragile_links(ds: Dataset, delta: float = DELTA, weights: WeightTable = HAND_TABLE) -> list[dict]:
    """One record per ledger link of `ds` (see `link_flips`), with its `link_id`."""
    if delta <= 0:
        raise ValueError("delta must be positive")
    ctx = ScoringContext.from_dataset(ds, weights)
    tables = _Tables(ctx, delta)
    out = []
    for link in ds.links:
        try:
            rec = link_flips(ctx, link.a, link.b, link.relation, delta, tables)
        except (KeyError, ValueError):
            continue  # a hand-made link between unknown or collapsed captures: nothing to perturb
        out.append({"link_id": link.id, **rec})
    return out


def flip_note(flips: list[dict], worst: Optional[dict], what: str = "") -> Optional[str]:
    """'fragile: flips to review if kit −2 bits; ...' (or the worst-case line), None if stable."""
    if flips:
        parts = [f"{what}to {f['to']} if {f['layer']} {_fmt(f['delta'])}" for f in flips]
        return "fragile: flips " + "; ".join(parts)
    if worst:
        return f"flips {what}to {worst['to']} only if every layer moves {_fmt(worst['delta'])}"
    return None


def _append(note: Optional[str], extra: str) -> str:
    return f"{note}; {extra}" if note else extra


def annotate_link(link: Link, rec: dict) -> Link:
    """The link with its sensitivity written into the notes of its ledger rows (contract is frozen)."""
    by_layer = defaultdict(list)
    for f in rec["flips"]:
        by_layer[f["layer"]].append(f)
    rows = []
    for row in link.evidence:
        fl = by_layer.pop(row.layer.value, None)
        note = flip_note(fl, None) if fl else None
        rows.append(row.model_copy(update={"note": _append(row.note, note)}) if note else row)
    if not rec["flips"] and rec["worst_case"] and rows:
        rows[0] = rows[0].model_copy(update={"note": _append(rows[0].note, flip_note([], rec["worst_case"]))})
    return link.model_copy(update={"evidence": rows})


def annotate_dataset(ds: Dataset, records: list[dict]) -> Dataset:
    by_id = {r["link_id"]: r for r in records}
    data = ds.model_dump(mode="json")
    data["links"] = [
        annotate_link(l, by_id[l.id]).model_dump(mode="json") if l.id in by_id else l.model_dump(mode="json") for l in ds.links
    ]
    return Dataset.model_validate(data)


def summary(records: list[dict], delta: float = DELTA) -> dict:
    """Flip counts per relation: links, fragile ones, worst-case flips, and flips per layer move."""
    per: dict[str, dict] = {}
    for r in records:
        s = per.setdefault(r["relation"], {"links": 0, "fragile": 0, "worst_case_flips": 0, "by_move": Counter()})
        s["links"] += 1
        s["fragile"] += r["fragile"]
        s["worst_case_flips"] += r["worst_case"] is not None
        for f in r["flips"]:
            s["by_move"][f"{f['layer']} {_fmt(f['delta'])} -> {f['to']}"] += 1
    for s in per.values():
        s["by_move"] = dict(sorted(s["by_move"].items(), key=lambda kv: (-kv[1], kv[0])))
    return {"delta_bits": delta, "relations": dict(sorted(per.items()))}


# --- assignments ---------------------------------------------------------------------------


def assignment_flips(
    base: Dataset,
    incident: Incident,
    assignment: Assignment,
    relation: Relation = Relation.campaign,
    index=None,
    delta: float = DELTA,
    weights: Optional[WeightTable] = None,
    backend=None,
) -> dict:
    """Re-run the assignment under each ±delta layer move and the worst case; record decision changes.

    Needs the hand-table ledger. With a `CandidateIndex` each re-run costs one candidate search.
    `weights` is the table to perturb (default: the index's, else the hand table); `backend` a
    factory for the linear scan (e.g. one carrying background counts), passed to `link_incident`.
    """
    from ftf.linking.assign import link_incident

    if weights is None:
        weights = index.weights if index is not None else HAND_TABLE
    state = (assignment.decision.value, assignment.campaign_id)

    def run(layers: tuple[Layer, ...], d: float) -> tuple[str, Optional[str]]:
        a = link_incident(base, incident, relation, index=index, backend=backend, weights=perturb(weights, layers, d)).assignment
        return a.decision.value, a.campaign_id

    flips = []
    for layer in SCORING_LAYERS:
        for d in (-delta, delta):
            s = run((layer,), d)
            if s != state:
                flips.append({"layer": layer.value, "delta": d, "to": _decision_text(s)})
    worst = None
    for d in (-delta, delta):
        s = run(SCORING_LAYERS, d)
        if s != state:
            worst = {"delta": d, "to": _decision_text(s)}
            break
    return {"incident_id": incident.id, "state": _decision_text(state), "flips": flips, "worst_case": worst, "fragile": bool(flips)}


def _decision_text(s: tuple[str, Optional[str]]) -> str:
    return f"{s[0]} {s[1]}" if s[1] else s[0]


def annotate_assignment(assignment: Assignment, rec: dict) -> Assignment:
    note = flip_note(rec["flips"], rec["worst_case"])
    if note is None:
        return assignment
    # The note goes on the assigned campaign's candidate, or on the top candidate otherwise.
    target = assignment.campaign_id or (assignment.candidates[0].campaign_id if assignment.candidates else None)
    cands = [
        c.model_copy(update={"note": _append(c.note, note)}) if c.campaign_id == target else c
        for c in assignment.candidates
    ]
    return assignment.model_copy(update={"note": _append(assignment.note, note), "candidates": cands})
