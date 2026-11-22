"""Glue for `ftf eval openset|degrade|distractors` (HACK-28): ground-truth groups from the labels,
the distractor plan, and the markdown tables written next to the JSON.

Nothing here reads organiser values itself. Which captures become which distractor is decided
by a fixed rule over the labels and the dataset (see `distractor_plan`), so the outputs can be
regenerated from the bucket without any value in git.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from ftf.contract import Dataset, Layer, Relation
from ftf.evaluate.perturb import (
    Distractor,
    brand_negatives,
    celebrity_only,
    collector_injection,
    kit_copy,
    natural_must_not_link,
    platform_only,
)
from ftf.evaluate.units import incident_of

#: How many campaign groups (largest first) supply a kit copy and a platform-only page each.
N_TEMPLATE_GROUPS = 3
#: Corpus tags that mark a real outlet page (the blocked fetches are not negatives).
BRAND_KINDS = ("must_not_link",)


def campaign_groups(labels: dict[str, Any]) -> dict[str, list[str]]:
    """Ground-truth groups for the campaign relation: label groups of two or more canonical captures.

    Several platform-id levels can give the same member set (campaign and landing ids often do);
    those are one group, else a correct assignment ties between two ids and is counted as wrong.
    The group id joins the label ids that share the member set.
    """
    by_members: dict[tuple[str, ...], list[str]] = {}
    for g in labels["groups"]:
        if g["relation"] == Relation.campaign.value and len(g["members"]) > 1:
            by_members.setdefault(tuple(sorted(g["members"])), []).append(g["id"])
    return {"+".join(sorted(ids)): list(members) for members, ids in by_members.items()}


def must_not_link_pairs(labels: dict[str, Any]) -> list[tuple[str, str]]:
    """Ground-truth must-not-link pairs for the campaign relation."""
    return [(p["a"], p["b"]) for p in labels["must_not_link"] if p["relation"] == Relation.campaign.value]


#: Creative-layer kinds used for (f). The organiser dataset has no face or image indicator, only
#: the ad id and ad name of the creative, so (f) takes every creative-layer indicator of a capture.
CREATIVE_KINDS = re.compile(r".")


def _has_creative(ds: Dataset, capture_id: str) -> bool:
    return any(
        i.layer is Layer.creative and CREATIVE_KINDS.search(i.kind)
        for i in ds.indicators() if i.capture_id == capture_id
    )


def distractor_plan(
    ds: Dataset,
    labels: dict[str, Any],
    corpus: Dataset,
    corpus_tags: dict[str, str],
    *,
    brand_limit: int | None = None,
) -> tuple[list[Distractor], dict[str, Any]]:
    """The E4 perturbations for this dataset, and notes on what was selected.

    Rules (all deterministic):
    - (a) every ground-truth must-not-link pair (campaign relation);
    - (b) and (c): the first capture of each of the `N_TEMPLATE_GROUPS` largest campaign groups;
    - (d) two disjoint pairs of first captures from different groups, each pair in two incidents;
    - (e) every corpus incident tagged `must_not_link` (optionally the first `brand_limit`);
    - (f) the first two canonical captures (sorted by id) that carry a creative-layer indicator,
      each in its own incident (the creative layer holds the ad id and name; no face indicator exists).
    """
    groups = sorted(campaign_groups(labels).values(), key=lambda m: (-len(m), m[0]))
    reps = [m[0] for m in groups]
    inc_of = incident_of(ds)
    plan: list[Distractor] = []

    plan += natural_must_not_link([([a], [b]) for a, b in must_not_link_pairs(labels)])
    # One capture can head more than one group (overlapping label levels): keep each capture once.
    template_reps = list(dict.fromkeys(reps))[:N_TEMPLATE_GROUPS]
    for n, cid in enumerate(template_reps):
        plan.append(kit_copy(ds, cid, tag=str(n)))
        plan.append(platform_only(ds, cid, tag=str(n)))

    used: set[str] = set()
    injected = 0
    for n, a in enumerate(reps):
        for b in reps[n + 1:]:
            if injected == 2:
                break
            if a in used or b in used or inc_of[a] == inc_of[b]:
                continue
            plan.append(collector_injection(ds, a, b, seed=injected))
            used |= {a, b}
            injected += 1

    faces: list[str] = []
    seen_incidents: set[str] = set()
    canon_ids = sorted({c.id for inc in ds.incidents for c in inc.captures if c.duplicate_of is None})
    for cid in canon_ids:
        if _has_creative(ds, cid) and inc_of[cid] not in seen_incidents:
            faces.append(cid)
            seen_incidents.add(inc_of[cid])
        if len(faces) == 2:
            break
    for n, cid in enumerate(faces):
        plan.append(celebrity_only(ds, cid, kinds=CREATIVE_KINDS, tag=str(n)))

    brand = brand_negatives(corpus, corpus_tags, kinds=BRAND_KINDS)
    brand_total = len(brand)
    if brand_limit is not None:
        brand = brand[:brand_limit]
    plan += brand

    notes = {
        "campaign_groups": len(groups),
        "must_not_link_pairs": len(must_not_link_pairs(labels)),
        "kit_copy_and_platform_only_captures": template_reps,
        "collector_injection_pairs": injected,
        "celebrity_only_captures": faces,
        "corpus_incidents": len(corpus.incidents),
        "corpus_tags": dict(Counter(corpus_tags.values())),
        "brand_negatives_tagged": brand_total,
        "brand_negatives_used": len(brand),
    }
    return plan, notes


def expectation_text(d: Distractor) -> str:
    """The expected answer of a distractor, in words."""
    e = d.expect
    parts = []
    if e.forbidden:
        rel = ", ".join(sorted(r.value for r in e.forbidden))
        target = "the paired capture" if e.against is not None else "outside captures"
        parts.append(f"no link or campaign of relation {rel} between the subjects and {target}")
    if e.never_assign:
        parts.append("never assigned (new or review)")
    if e.decisions_unchanged:
        parts.append("zero decisions change")
    if e.expected:
        rel = ", ".join(sorted(r.value for r in e.expected))
        parts.append(f"expected {rel} link to the original (reported, not required)")
    return "; ".join(parts)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _table(headers: list[str], rows: Iterable[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def openset_markdown(title: str, payload: dict[str, Any]) -> str:
    """Raw counts per (protocol, mode); rates in brackets, never a single accuracy."""
    head = [
        f"# {title}",
        "",
        f"dataset build `{payload['dataset_build']}`, engine `{payload['engine']}`, relation "
        f"`{payload['relation']}`, {len(payload['groups'])} ground-truth groups (2+ captures).",
        "",
    ]
    rows = [
        [
            r["protocol"], r["mode"], r["items"], r["correct"], r["new"], r["review"],
            f"{r['wrong']} of {r['items']} ({_pct(r['false_accept_rate'])})",
            f"{r['assigned']} ({_pct(r['assign_rate'])})",
            _num(r["median_evidence_capacity_bits"]),
        ]
        for r in payload["summary"]
    ]
    table = _table(
        ["protocol", "mode", "items", "correct", "new", "review", "wrong = false accepts", "assigned", "median capacity (bits)"],
        rows,
    )
    return "\n".join(head + table) + "\n"


def distractor_markdown(payload: dict[str, Any]) -> str:
    """One line per distractor with its expected answer and its result."""
    rows = payload["results"]
    passed = sum(1 for r in rows if r["passed"])
    head = [
        "# E4 distractors",
        "",
        f"dataset build `{payload['dataset_build']}`, engine `{payload['engine']}`: "
        f"{passed} of {len(rows)} distractors pass.",
        "",
    ]
    by_code = Counter(r["code"] for r in rows)
    fails = Counter(r["code"] for r in rows if not r["passed"])
    summary = _table(
        ["code", "distractors", "passed", "failed"],
        [[c, by_code[c], by_code[c] - fails[c], fails[c]] for c in sorted(by_code)],
    )
    detail = _table(
        ["code", "distractor", "subjects", "result", "expected", "violations", "decisions"],
        [
            [
                r["code"], r["name"], len(r["subjects"]), "pass" if r["passed"] else "FAIL",
                r["expected"], len(r["violations"]), ", ".join(f"{k}: {v}" for k, v in r["decisions"].items()) or "-",
            ]
            for r in rows
        ],
    )
    mnl = payload["must_not_link"]
    tail = [
        "",
        f"Reference partition vs ground-truth must-not-link pairs (campaign): "
        f"{mnl['violated']} of {mnl['pairs']} pairs in one predicted campaign.",
    ]
    return "\n".join(head + summary + ["", "Per distractor:", ""] + detail + tail) + "\n"
