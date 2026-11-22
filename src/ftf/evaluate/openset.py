"""Open-set evaluation (E2), degraded re-runs (E3) and distractor runs (E4).

Duck-typed against any engine with
    link_dataset(ds: Dataset) -> Dataset                         # builds the base
    link_incident(base, incident, relation=Relation.campaign)    # -> .assignment, .links
so it imports no engine code. Ground truth is a plain mapping `groups`: group id -> capture ids
(capture ids are never hard-coded here; they come with the labels).

Protocols (plans 07 §4 E2, 05 §5):
- loo_item: hold out one incident whose groups keep members elsewhere, cluster the rest,
  assign the held-out incident.
- loco: hold out every incident that holds a capture of one group, cluster the rest, assign
  each held-out incident. They should come out `new`.

Unit of hold-out is the incident, because `assign` takes an incident. An incident can hold
captures of several groups (folder 2 mixes A1 and B). It is held out once per protocol run:
in loo_item it is one item whose *known groups* are all its groups that keep a member in the
reference; in loco, holding out group g also removes the other captures of the incidents that
hold g. Either way an `assign` is correct when the chosen campaign maps to one of the item's
known groups (see `campaign_group`), and wrong otherwise. Exact duplicates (`duplicate_of`
either way) of a held-out capture are held out with it, so a re-capture cannot leak the answer.

Report raw counts ("3 of 15"), never a single accuracy number; ~15 items is a regression suite,
not a benchmark.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ftf.contract import Assignment, Dataset, Decision, Incident, Link, Relation
from ftf.evaluate.perturb import (
    MODES,
    Distractor,
    assignment_violations,
    decision_diff,
    degrade_incident,
    expected_met,
    link_violations,
    replace_incidents,
    with_incidents,
)

PROTOCOLS = ("loo_item", "loco")
FULL = "full"  # mode name for the undegraded item


class IncidentResultLike(Protocol):
    assignment: Assignment
    links: list[Link]


class EngineLike(Protocol):
    def link_dataset(self, ds: Dataset) -> Dataset: ...
    def link_incident(self, base: Dataset, incident: Incident, relation: Relation = Relation.campaign) -> IncidentResultLike: ...


@dataclass
class ItemResult:
    protocol: str  # loo_item | loco
    mode: str  # full or a key of MODES
    held_out: str  # group id (loco) or incident id (loo_item) that defines the run
    incident_id: str
    true_groups: list[str]  # all groups of the incident's captures
    known_groups: list[str]  # those that keep members in the reference
    decision: str  # assign | new | review
    campaign_id: str | None
    campaign_groups: list[str]  # groups the chosen campaign maps to
    outcome: str  # correct | wrong | new | review
    evidence_capacity_bits: float
    top_score: float | None  # best candidate score, when the engine exposes candidates
    top_correct: bool | None  # whether that best candidate maps to a known group
    note: str = ""

    @property
    def false_accept(self) -> bool:
        return self.outcome == "wrong"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def _group_of(groups: Mapping[str, Sequence[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for g, caps in groups.items():
        for c in caps:
            out.setdefault(c, set()).add(g)
    return out


def campaign_group(capture_ids: Iterable[str], group_of: Mapping[str, set[str]]) -> list[str]:
    """Groups a campaign maps to: the plurality group of its labelled captures.

    Unlabelled captures are ignored. A tie maps to several groups, and an `assign` to such a
    campaign counts as wrong (the campaign is impure, so the answer is not identifiable).
    """
    votes: Counter[str] = Counter()
    for c in capture_ids:
        for g in group_of.get(c, ()):
            votes[g] += 1
    if not votes:
        return []
    top = max(votes.values())
    return sorted(g for g, n in votes.items() if n == top)


def _hold_out_set(ds: Dataset, incident_ids: Iterable[str]) -> set[str]:
    """Incident ids to remove: the given ones plus incidents whose every capture is an exact
    duplicate of (or the canonical of) a capture being removed."""
    drop = set(incident_ids)
    canon = {c.id: (c.duplicate_of or c.id) for i in ds.incidents for c in i.captures}
    while True:
        roots = {canon[c.id] for i in ds.incidents if i.id in drop for c in i.captures}
        more = {
            i.id for i in ds.incidents
            if i.id not in drop and all(canon[c.id] in roots for c in i.captures)
        }
        if not more:
            return drop
        drop |= more


def _judge(
    *,
    protocol: str,
    mode: str,
    held_out: str,
    incident: Incident,
    rest: Dataset,
    base: Dataset,
    result: IncidentResultLike,
    group_of: Mapping[str, set[str]],
) -> ItemResult:
    a = result.assignment
    note = ""
    if a.incident_id != incident.id:
        note = f"engine returned assignment for {a.incident_id}"
    true = sorted({g for c in incident.captures for g in group_of.get(c.id, ())})
    ref_caps = {c.id for i in rest.incidents for c in i.captures}
    known = sorted(g for g in true if any(g in group_of.get(c, ()) for c in ref_caps))
    camps = {c.id: c for c in base.campaigns}

    def maps(cid: str | None) -> list[str]:
        camp = camps.get(cid) if cid else None
        return campaign_group(camp.capture_ids, group_of) if camp else []

    def ok(cg: list[str]) -> bool:
        return len(cg) == 1 and cg[0] in known

    cg = maps(a.campaign_id)
    if a.decision is Decision.assign:
        if a.campaign_id not in camps:
            note = (note + "; " if note else "") + "assigned to a campaign not in the base"
        outcome = "correct" if ok(cg) else "wrong"
    else:
        outcome = a.decision.value
    top_score = top_correct = None
    if a.candidates:
        best = max(a.candidates, key=lambda c: c.score_bits)
        top_score = best.score_bits
        top_correct = ok(maps(best.campaign_id))
    return ItemResult(
        protocol=protocol, mode=mode, held_out=held_out, incident_id=incident.id,
        true_groups=true, known_groups=known, decision=a.decision.value, campaign_id=a.campaign_id,
        campaign_groups=cg, outcome=outcome, evidence_capacity_bits=a.evidence_capacity_bits,
        top_score=top_score, top_correct=top_correct, note=note,
    )


# --------------------------------------------------------------------------------------------
# E2 / E3 runner
# --------------------------------------------------------------------------------------------

def _runs(ds: Dataset, groups: Mapping[str, Sequence[str]], protocols: Sequence[str]):
    """Yield (protocol, held_out label, incident ids to test, incident ids to remove)."""
    unknown = set(protocols) - set(PROTOCOLS)
    if unknown:
        raise ValueError(f"unknown protocols {sorted(unknown)}")
    group_of = _group_of(groups)
    inc_of = {c.id: i.id for i in ds.incidents for c in i.captures}
    if "loo_item" in protocols:
        for inc in ds.incidents:
            gs = {g for c in inc.captures for g in group_of.get(c.id, ())}
            if not gs:
                continue
            drop = _hold_out_set(ds, [inc.id])
            rest_caps = {c.id for i in ds.incidents if i.id not in drop for c in i.captures}
            if any(set(groups[g]) & rest_caps for g in gs):
                yield "loo_item", inc.id, [inc.id], drop
    if "loco" in protocols:
        for g, caps in groups.items():
            members = sorted({inc_of[c] for c in caps if c in inc_of})
            if members:
                yield "loco", g, members, _hold_out_set(ds, members)


def run_openset(
    engine: EngineLike,
    dataset: Dataset,
    groups: Mapping[str, Sequence[str]],
    *,
    modes: Sequence[str] = (FULL, *MODES),
    protocols: Sequence[str] = PROTOCOLS,
    relation: Relation = Relation.campaign,
) -> list[ItemResult]:
    """Run leave-one-out (item and whole group) on `dataset`, the held-out item full and degraded.

    The reference is never degraded; only the held-out incident is (E3). The base is clustered
    once per hold-out and reused for every mode.
    """
    unknown = {c for caps in groups.values() for c in caps} - {c.id for i in dataset.incidents for c in i.captures}
    if unknown:
        raise KeyError(f"groups name captures not in the dataset: {sorted(unknown)[:5]}")
    for m in modes:
        if m != FULL and m not in MODES:
            raise ValueError(f"unknown mode {m!r}")
    group_of = _group_of(groups)
    by_id = {i.id: i for i in dataset.incidents}
    results: list[ItemResult] = []
    for protocol, label, tested, drop in _runs(dataset, groups, protocols):
        rest = with_incidents(dataset, drop=drop, name=f"{dataset.name}-{protocol}-{label}")
        base = engine.link_dataset(rest)
        for iid in tested:
            for mode in modes:
                item = by_id[iid] if mode == FULL else degrade_incident(by_id[iid], mode)
                res = engine.link_incident(base, item, relation=relation)
                results.append(_judge(
                    protocol=protocol, mode=mode, held_out=label, incident=item, rest=rest, base=base,
                    result=res, group_of=group_of,
                ))
    return results


def _of(k: int, n: int) -> str:
    return f"{k} of {n}"


def summarise(results: Iterable[ItemResult]) -> list[dict[str, Any]]:
    """Counts per (protocol, mode): outcomes, assign rate, false accepts, median capacity.

    Rates are given next to raw counts and are None when there is nothing to count.
    """
    buckets: dict[tuple[str, str], list[ItemResult]] = {}
    for r in results:
        buckets.setdefault((r.protocol, r.mode), []).append(r)
    rows = []
    for (protocol, mode), rs in sorted(buckets.items()):
        n = len(rs)
        oc = Counter(r.outcome for r in rs)
        assigned = oc["correct"] + oc["wrong"]
        fa = oc["wrong"]
        caps = [r.evidence_capacity_bits for r in rs]
        rows.append({
            "protocol": protocol,
            "mode": mode,
            "items": n,
            "correct": oc["correct"],
            "wrong": oc["wrong"],
            "new": oc["new"],
            "review": oc["review"],
            "assigned": _of(assigned, n),
            "assign_rate": assigned / n if n else None,
            "false_accepts": _of(fa, n),
            "false_accept_rate": fa / n if n else None,
            "median_evidence_capacity_bits": statistics.median(caps) if caps else None,
        })
    return rows


def threshold_sweep(results: Iterable[ItemResult], mode: str = FULL) -> list[dict[str, Any]]:
    """Correct-assignment vs false-accept curve (OSCR-like) from candidate scores.

    At threshold t an item is accepted into its best candidate if that candidate's score >= t;
    this re-decides from the candidates and ignores the engine's own decision. Known items are
    loo_item results with a known group; unknown items are loco results with no known group.
    correct: known items whose accepted best candidate is right; false accepts: unknown items
    accepted, plus known items accepted into a wrong campaign. Items without candidates are
    never accepted. Empty if no item exposes candidate scores.
    """
    rs = [r for r in results if r.mode == mode]
    if not any(r.top_score is not None for r in rs):
        return []
    known = [r for r in rs if r.protocol == "loo_item" and r.known_groups]
    unknown = [r for r in rs if r.protocol == "loco" and not r.known_groups]
    # The last threshold, None, accepts nothing (kept JSON-safe instead of infinity).
    thresholds: list[float | None] = sorted({r.top_score for r in rs if r.top_score is not None}) + [None]
    rows = []
    for t in thresholds:
        acc = lambda r: t is not None and r.top_score is not None and r.top_score >= t
        correct = sum(1 for r in known if acc(r) and r.top_correct)
        wrong_known = sum(1 for r in known if acc(r) and not r.top_correct)
        fa_unknown = sum(1 for r in unknown if acc(r))
        rows.append({
            "mode": mode,
            "threshold_bits": t,
            "correct": _of(correct, len(known)),
            "correct_rate": correct / len(known) if known else None,
            "false_accepts_unknown": _of(fa_unknown, len(unknown)),
            "false_accept_rate": fa_unknown / len(unknown) if unknown else None,
            "wrong_known": _of(wrong_known, len(known)),
        })
    return rows


# --------------------------------------------------------------------------------------------
# E4 runner
# --------------------------------------------------------------------------------------------

@dataclass
class DistractorResult:
    code: str
    name: str
    subjects: list[str]
    violations: list[str] = field(default_factory=list)
    expected_met: bool | None = None
    decisions: dict[str, str] = field(default_factory=dict)  # incident id -> decision

    @property
    def passed(self) -> bool:
        return not self.violations


def _as_dataset(base: Dataset, links: Iterable[Link]) -> Dataset:
    """A link-only view for the link checks (no validation needed: we only read fields)."""
    return Dataset.model_construct(name=base.name, incidents=[], links=list(links), campaigns=[], assignments=[])


def run_distractors(
    engine: EngineLike,
    reference: Dataset,
    distractors: Iterable[Distractor],
    *,
    relation: Relation = Relation.campaign,
) -> list[DistractorResult]:
    """Check each distractor against the engine.

    - added incidents (b, c, e, f): cluster reference + incident and check links and campaigns;
      assign the incident against the plain base and check its links and decision;
    - replacements (d): cluster with and without the swap, and assign each swapped incident
      against the rest (original vs perturbed); any difference is a violation;
    - no incidents (a): check the base.
    """
    plain = with_incidents(reference)
    base = engine.link_dataset(plain)
    out = []
    for d in distractors:
        r = DistractorResult(code=d.code, name=d.name, subjects=sorted(d.subjects))
        if d.replaces:
            swapped = replace_incidents(plain, d.incidents)
            after = engine.link_dataset(swapped)
            r.violations += decision_diff(base, after)
            for inc in d.incidents:
                orig = next(i for i in plain.incidents if i.id == inc.id)
                rest_before = engine.link_dataset(with_incidents(plain, drop=[inc.id]))
                rest_after = engine.link_dataset(with_incidents(swapped, drop=[inc.id]))
                a0 = engine.link_incident(rest_before, orig, relation=relation)
                a1 = engine.link_incident(rest_after, inc, relation=relation)
                r.decisions[inc.id] = a1.assignment.decision.value
                v0 = _assign_view(a0, rest_before)
                v1 = _assign_view(a1, rest_after)
                if v0 != v1:
                    r.violations.append(f"assignment of {inc.id} changed")
        elif d.incidents:
            combined = engine.link_dataset(with_incidents(plain, add=d.incidents))
            r.violations += link_violations(d, combined)
            r.expected_met = expected_met(d, combined)
            for inc in d.incidents:
                res = engine.link_incident(base, inc, relation=relation)
                r.decisions[inc.id] = res.assignment.decision.value
                r.violations += [f"assign-time {v}" for v in link_violations(d, _as_dataset(base, res.links))]
                r.violations += assignment_violations(d, res.assignment, base)
        else:
            r.violations += link_violations(d, base)
        out.append(r)
    return out


def _assign_view(res: IncidentResultLike, base: Dataset) -> tuple:
    a = res.assignment
    camp = next((c for c in base.campaigns if c.id == a.campaign_id), None)
    links = {(frozenset((l.a, l.b)), l.relation.value, l.level.value) for l in res.links}
    return a.decision.value, frozenset(camp.capture_ids) if camp else None, frozenset(links)


def to_jsonable(rows: Iterable[ItemResult | DistractorResult]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = asdict(r)
        if isinstance(r, ItemResult):
            d["false_accept"] = r.false_accept
        else:
            d["passed"] = r.passed
        out.append(d)
    return out


__all__ = [
    "FULL",
    "PROTOCOLS",
    "DistractorResult",
    "EngineLike",
    "ItemResult",
    "campaign_group",
    "run_distractors",
    "run_openset",
    "summarise",
    "threshold_sweep",
    "to_jsonable",
]
