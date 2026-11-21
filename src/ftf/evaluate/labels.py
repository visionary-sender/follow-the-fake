"""Ground truth for evaluation: groups of captures and must-not-link pairs, each with a reason.

Two sources:

- `platform_id`: the scammers' own platform ids (`window.aio` config: campaign, owner, tenant,
  lander template; landing-cdn tenant/landing path segments). Derived from the dataset's
  indicators of the kinds in `PLATFORM_LEVELS`; captures sharing a value form a group.
- `analyst_verdict`: groups and must-not-link pairs from the cross-case analysis (planning
  knowledge 04 §(c), 07 §4 E4(a)), written down in `ANALYST_GROUPS` / `MUST_NOT_LINK` by
  prototype node name and mapped to capture ids through `ftf.evaluate.units`.

Labels are on canonical captures only (a duplicate is the same unit as the capture it copies).
`compare_expected` checks the derived platform-id groups against the expected E1 groups (07 §4)
and returns every discrepancy instead of forcing a match.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ftf.contract import Dataset, Relation
from ftf.evaluate.units import canonical, node_to_capture, source_keys

LABELS_SCHEMA = "ftf-eval-labels@0.1"

# level -> (indicator kind carrying the platform id, engine relation the level is compared with, meaning)
PLATFORM_LEVELS: dict[str, tuple[str, Relation, str]] = {
    "campaign": ("aio_campaign", Relation.campaign, "same aio campaign id"),
    "owner": ("aio_owner", Relation.operator, "same aio owner (account) id"),
    "tenant": ("aio_cdn_tenant", Relation.operator, "same landing-cdn tenant path segment"),
    "landing": ("aio_landing", Relation.campaign, "same aio landing id"),
    "template": ("aio_lander_template", Relation.template, "same aio lander template id"),
}

# Analyst verdicts (04 §(c)); members are prototype node names. level -> list of (members, relation, reason).
ANALYST_GROUPS: dict[str, list[tuple[tuple[str, ...], Relation, str]]] = {
    "analyst_campaign": [
        (("2/healthlinkq", "3"), Relation.campaign,
         "the healthlinkq HAR in folder 2 is case 3's own visit (same landing chain)"),
    ],
    "analyst_operator": [
        (("2/healthlinkq", "3", "p5"), Relation.operator,
         "group B: same Binom tracker instance (xid machine) and crimsonbase tracker host"),
    ],
    "analyst_template": [
        (("p8", "p11"), Relation.template,
         "identical 'Trudna odpowiedz' FinNews template assets and text; different platforms"),
        (("47", "p9", "p10"), Relation.template,
         "group C: same nutra VSL lander kit (init.js, fb-order.php); no shared operator id, kit level only"),
        (("48", "61/s1", "61/s2", "61/s3"), Relation.template,
         "same lander kit, byte-identical news/avatar images, same headline and byline (07 §4: template, not operator)"),
    ],
}

# Natural must-not-link pairs (07 §4 E4(a), 04 §(c)): (side a, side b, relation, reason). "*" = every other capture.
MUST_NOT_LINK: list[tuple[tuple[str, ...], tuple[str, ...], Relation, str]] = [
    (("p5",), ("48", "61/s1", "61/s2", "61/s3"), Relation.campaign,
     "shared creative supply only (bank screenshots, news photos); different tracker stack"),
    (("p8",), ("p11",), Relation.operator, "same template on different platforms (PHP lander vs aio)"),
    (("14",), ("47", "p10"), Relation.campaign, "stock nutra assets only; different pixels and trackers"),
    (("3",), ("1",), Relation.campaign, "same narrative only; different tracker infrastructure"),
    (("36",), ("p8",), Relation.campaign, "base-href shape only"),
    (("2/acrosscommunities",), ("2/healthlinkq",), Relation.campaign,
     "two different sites saved in one folder; no infra or id family links them"),
    (("p14",), ("*",), Relation.campaign, "English-language outlier funnel, unrelated to every other case"),
]

# Expected E1 groups (07 §4) by node; "61" stands for every case-61 capture that carries the id.
_A = {"p1", "p7", "p15", "1", "2/acrosscommunities"}
_A2 = {"p3", "p4"}
EXPECTED_E1: dict[str, list[set[str]]] = {
    "campaign": [_A, _A2, {"p6"}, {"p12"}, {"p11"}, {"48"}, {"61"}],
    "owner": [_A | _A2, {"p6", "p12"}, {"48"}, {"61"}, {"p11"}],
    "tenant": [_A | _A2, {"48", "61"}, {"p6", "p12"}, {"p11"}],
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


def build_labels(ds: Dataset) -> dict[str, Any]:
    """Ground-truth document (see docs/evaluation.md for the schema)."""
    canon = canonical(ds)
    keys = source_keys(ds)
    caps = {c for c in canon if canon[c] == c}
    groups: list[dict[str, Any]] = []

    def add_group(level: str, members: list[str], relation: Relation, source: str, reason: str, value: list[str] | None = None) -> None:
        groups.append({
            "id": f"{level}:{len([g for g in groups if g['level'] == level])}",
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
            if ind.kind == kind and ind.value is not None:
                vals[canon[ind.capture_id]].add(ind.value)
        for comp in _components(vals):
            shared = sorted(set.union(*(vals[c] for c in comp)))
            reason = f"{meaning}" + ("" if len(comp) > 1 else " (no other capture carries it)")
            add_group(level, comp, relation, "platform_id", reason, shared)

    missing: list[str] = []

    def resolve(nodes: tuple[str, ...]) -> list[str]:
        out = []
        for n in nodes:
            cid = node_to_capture(n)
            if cid in canon:
                out.append(canon[cid])
            else:
                missing.append(n)
        return sorted(set(out))

    for level, entries in ANALYST_GROUPS.items():
        for nodes, relation, reason in entries:
            members = resolve(nodes)
            if members:
                add_group(level, members, relation, "analyst_verdict", reason)

    pairs: list[dict[str, Any]] = []
    for side_a, side_b, relation, reason in MUST_NOT_LINK:
        a_caps = resolve(side_a)
        b_caps = sorted(caps - set(a_caps)) if side_b == ("*",) else resolve(side_b)
        for a in a_caps:
            for b in b_caps:
                if a != b:
                    pairs.append({"a": a, "b": b, "relation": relation.value, "source": "analyst_verdict", "reason": reason})

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
        "must_not_link_pairs": len(pairs),
        "unresolved_nodes": sorted(set(missing)),
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


def _project(members: list[str]) -> frozenset[str]:
    """Capture ids -> node names, with every case-61 capture collapsed onto "61"."""
    from ftf.evaluate.units import _NODE_KEYS  # noqa: PLC0415 - one mapping, read in reverse

    reverse = {node_to_capture(n): n for n in _NODE_KEYS}
    out = set()
    for m in members:
        node = reverse.get(m)
        if node is None:
            inc, _, key = m.rpartition(":")
            node = inc.split(":", 1)[1] if key == "c1" else m
        out.add("61" if node.startswith("61") else node)
    return frozenset(out)


def compare_expected(labels: dict[str, Any]) -> list[str]:
    """Discrepancies between derived platform-id groups and the expected E1 groups (07 §4)."""
    report = []
    for level, expected in EXPECTED_E1.items():
        got = {_project(m) for m in partition(labels, level)}
        want = {frozenset(e) for e in expected}
        for g in sorted(got - want, key=sorted):
            report.append(f"{level}: derived group {sorted(g)} is not an expected E1 group")
        for w in sorted(want - got, key=sorted):
            report.append(f"{level}: expected E1 group {sorted(w)} not derived")
    return report
