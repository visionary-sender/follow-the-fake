"""Ground truth for evaluation: groups of captures and must-not-link pairs, each with a reason.

Two sources:

- `platform_id`: the scammers' own platform ids (campaign, owner, tenant, landing, lander
  template), read from the dataset's indicators of the kinds in `PLATFORM_LEVELS`; captures
  that share a value form a group (connected components).
- `analyst_verdict`: groups and must-not-link pairs from the cross-case analysis, read from the
  private ground-truth spec (`ftf.evaluate.units.GroundTruthSpec`) by capture id.

Labels are on canonical captures only (a duplicate is the same unit as the capture it copies).
A spec entry naming a capture that is not in the dataset is reported in
`summary.missing_captures`, never silently dropped. `compare_expected` checks the derived
platform-id groups against the spec's expected E1 groups and returns every discrepancy instead
of forcing a match.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ftf.contract import Dataset, Relation
from ftf.evaluate.units import GroundTruthSpec, canonical, incident_of, source_keys

LABELS_SCHEMA = "ftf-eval-labels@0.2"

# level -> (indicator kind carrying the platform id, engine relation the level is compared with, meaning)
PLATFORM_LEVELS: dict[str, tuple[str, Relation, str]] = {
    "campaign": ("aio_campaign", Relation.campaign, "same platform campaign id"),
    "owner": ("aio_owner", Relation.operator, "same platform owner (account) id"),
    "tenant": ("aio_cdn_tenant", Relation.operator, "same landing-cdn tenant"),
    "landing": ("aio_landing", Relation.campaign, "same platform landing id"),
    "template": ("aio_lander_template", Relation.template, "same platform lander template id"),
}


def _components(values: dict[str, set[str]]) -> list[list[str]]:
    """Connected components of captures that share any value (union-find)."""
    parent = {c: c for c in values}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, str] = {}
    for cap, vals in sorted(values.items()):
        for v in sorted(vals):
            if v in owner:
                parent[find(cap)] = find(owner[v])
            else:
                owner[v] = cap
    groups: dict[str, list[str]] = defaultdict(list)
    for cap in values:
        groups[find(cap)].append(cap)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def build_labels(ds: Dataset, spec: GroundTruthSpec) -> dict[str, Any]:
    """Ground-truth document (see docs/evaluation.md for the schema)."""
    canon = canonical(ds)
    keys = source_keys(ds)
    caps = {c for c in canon if canon[c] == c}
    groups: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []

    def add_group(level: str, members: list[str], relation: Relation, source: str, reason: str,
                  value: list[str] | None = None) -> None:
        groups.append({
            "id": f"{level}:{sum(1 for g in groups if g['level'] == level)}",
            "level": level,
            "relation": relation.value,
            "source": source,
            "members": members,
            "member_keys": [keys[m].as_dict() for m in members],
            "reason": reason,
            **({"platform_values": value} if value is not None else {}),
        })

    for level, (kind, relation, meaning) in PLATFORM_LEVELS.items():
        vals: dict[str, set[str]] = defaultdict(set)
        for ind in ds.indicators():
            if ind.kind == kind and ind.value:
                vals[canon[ind.capture_id]].add(ind.value)
        for comp in _components(vals):
            shared = sorted(set.union(*(vals[c] for c in comp)))
            reason = meaning + ("" if len(comp) > 1 else " (no other capture carries it)")
            add_group(level, comp, relation, "platform_id", reason, shared)

    def resolve(ids: list[str], where: str) -> list[str]:
        out = []
        for cid in ids:
            if cid in canon:
                out.append(canon[cid])
            else:
                missing.append({"capture": cid, "in": where})
        return sorted(set(out))

    for level, entries in spec.analyst_groups.items():
        if level in PLATFORM_LEVELS:
            raise ValueError(f"analyst level {level!r} collides with a platform-id level; rename it in the spec")
        relations = sorted({e["relation"] for e in entries})
        if len(relations) > 1:
            raise ValueError(f"analyst level {level!r} mixes relations {relations}; use one level per relation")
        for n, entry in enumerate(entries):
            members = resolve(entry["members"], f"analyst_groups.{level}[{n}]")
            if members:
                add_group(level, members, Relation(entry["relation"]), "analyst_verdict", entry["reason"])

    pairs: list[dict[str, Any]] = []
    for n, entry in enumerate(spec.must_not_link):
        relation = Relation(entry["relation"])
        a_caps = resolve(entry["a"], f"must_not_link[{n}].a")
        b_caps = sorted(caps - set(a_caps)) if entry["b"] == ["*"] else resolve(entry["b"], f"must_not_link[{n}].b")
        for a in a_caps:
            for b in b_caps:
                if a != b:
                    pairs.append({"a": a, "b": b, "relation": relation.value, "source": "analyst_verdict",
                                  "reason": entry["reason"]})

    levels = sorted({g["level"] for g in groups})
    summary = {
        "captures": len(canon),
        "canonical_captures": len(caps),
        "groups_per_level": {
            lv: {
                "groups": sum(1 for g in groups if g["level"] == lv),
                "non_singleton": sum(1 for g in groups if g["level"] == lv and len(g["members"]) > 1),
                "captures": sum(len(g["members"]) for g in groups if g["level"] == lv),
            }
            for lv in levels
        },
        "platform_levels_without_indicators": [lv for lv in PLATFORM_LEVELS if lv not in levels],
        "must_not_link_pairs": len(pairs),
        "missing_captures": missing,
    }
    return {
        "schema": LABELS_SCHEMA,
        "dataset": ds.name,
        "captures": {c: {**keys[c].as_dict(), "canonical": canon[c]} for c in sorted(canon)},
        "groups": groups,
        "must_not_link": pairs,
        "summary": summary,
    }


def partition(labels: dict[str, Any], level: str) -> list[list[str]]:
    """The label partition of one level: list of member lists (its universe is their union)."""
    return [g["members"] for g in labels["groups"] if g["level"] == level]


def _project(members: list[str], spec: GroundTruthSpec, incident: dict[str, str]) -> frozenset[str]:
    """Captures of a `collapse_incidents` incident collapse onto the incident id."""
    collapse = set(spec.collapse_incidents)
    return frozenset(incident[m] if incident.get(m) in collapse else m for m in members)


def compare_expected(labels: dict[str, Any], spec: GroundTruthSpec, ds: Dataset) -> list[str]:
    """Discrepancies between derived platform-id groups and the spec's expected E1 groups."""
    incident = incident_of(ds)
    report = []
    for level, expected in spec.expected_e1.items():
        got = {_project(m, spec, incident) for m in partition(labels, level)}
        want = {_project(list(e), spec, incident) for e in expected}
        for g in sorted(got - want, key=sorted):
            report.append(f"{level}: derived group {sorted(g)} is not an expected E1 group")
        for w in sorted(want - got, key=sorted):
            report.append(f"{level}: expected E1 group {sorted(w)} not derived")
    return report
