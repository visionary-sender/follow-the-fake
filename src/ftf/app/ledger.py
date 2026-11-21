"""Evidence ledger and result tables as plain data (no Streamlit), for the app, HACK-31 and the README.

Vocabulary follows the CERT Polska evidence tiers NASK analysts use: link level `strict` is
**Strong**, `medium` **Medium**, `loose` **Supplementary**; a score below the review threshold is
**Insufficient alone**. Every value taken from a capture goes through `display_value`, which
defangs URLs and domains (`hxxp`, `[.]`) and truncates, so nothing shown is clickable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ftf.contract import (
    Assignment,
    Dataset,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Link,
    LinkLevel,
    MissingModality,
    Relation,
)

LEVEL_LABELS = {LinkLevel.strict: "Strong", LinkLevel.medium: "Medium", LinkLevel.loose: "Supplementary"}
INSUFFICIENT = "Insufficient alone"
RELATION_LABELS = {
    Relation.template: "same template",
    Relation.tracking_account: "same tracking account",
    Relation.operator: "same operator",
    Relation.campaign: "same campaign",
    Relation.ecosystem: "same ecosystem",
}
STATUS_LABELS = {
    EvidenceStatus.shared: "shared",
    EvidenceStatus.conflict: "conflict",
    EvidenceStatus.missing: "missing",
    EvidenceStatus.hub_subsumed: "hub-subsumed",
    EvidenceStatus.collector_excluded: "collector-excluded",
}
STATUS_NOTES = {
    EvidenceStatus.missing: "not available — not a penalty",
    EvidenceStatus.collector_excluded: "describes the collector, not the scam",
    EvidenceStatus.hub_subsumed: "common hub value; counted at a coarser level",
}
DECISION_LABELS = {"assign": "Assign to campaign", "new": "New campaign", "review": "Needs review"}
# Display thresholds; the linking engine owns the real ones (the fixture uses 10 / 4 bits).
REVIEW_THRESHOLD_BITS = 4.0
ASSIGN_THRESHOLD_BITS = 10.0
# Upper bound of what each missing input could add, by layer. Illustrative priors (method doc §D2)
# until linking reports a per-modality capacity.
MODALITY_BITS = {
    "har": (6.0, "tracking and infrastructure layers"),
    "html": (4.0, "kit and landing layers"),
    "mhtml": (4.0, "kit and landing layers"),
    "ad_screenshot": (2.0, "creative layer"),
    "screenshot": (2.0, "creative layer"),
    "video": (2.0, "creative layer"),
    "ad_library": (3.0, "distribution account layer"),
}
MAX_VALUE_CHARS = 60

_URLISH = re.compile(r"(?i)\b(?:https?|ftp)://|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")


def defang(text: str) -> str:
    """`https://a.example/x` → `hxxps://a[.]example/x`. Leaves text without URLs or domains alone."""
    if not _URLISH.search(text):
        return text
    out = re.sub(r"(?i)\bhttp", "hxxp", text)
    out = re.sub(r"(?i)\bftp://", "fxp://", out)
    return re.sub(r"(?<=[A-Za-z0-9-])\.(?=[A-Za-z0-9-])", "[.]", out)


def truncate(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def display_value(value: Optional[str], limit: int = MAX_VALUE_CHARS) -> str:
    """Safe display form of a captured value: defanged, one line, truncated."""
    if value is None:
        return ""
    return truncate(defang(value), limit)


def fmt_bits(bits: Optional[float]) -> str:
    if bits is None:
        return "n/a"
    return f"{bits:+.1f} bits"


def tier(link: Link, review_threshold: float = REVIEW_THRESHOLD_BITS) -> str:
    """Evidence tier label of a link."""
    if link.score_bits is not None and link.score_bits < review_threshold:
        return INSUFFICIENT
    return LEVEL_LABELS[link.level]


def _indicator_text(ind: Indicator) -> str:
    if ind.value is not None:
        return ind.value
    ref = ind.vector_ref
    return f"vector ({ref.model}, {ref.dim}-d)" if ref else ""


def seen_in(population: Dataset, kind: str, value: Optional[str]) -> str:
    """'k / N': incidents of `population` holding `kind` = `value`, out of all incidents."""
    total = len(population.incidents)
    if value is None:
        return f"? / {total}"
    k = sum(1 for inc in population.incidents if any(i.kind == kind and i.value == value for i in inc.indicators))
    return f"{k} / {total}"


@dataclass(frozen=True)
class LedgerRow:
    layer: str
    kind: str
    value: str
    seen_in: str
    bits: float
    status: str
    note: str
    greyed: bool

    def as_dict(self) -> dict:
        return {
            "layer": self.layer,
            "indicator": self.kind,
            "value": self.value,
            "seen in": self.seen_in,
            "bits": round(self.bits, 2),
            "status": self.status,
            "note": self.note,
        }


def ledger_rows(link: Link, dataset: Dataset, population: Dataset | None = None) -> list[LedgerRow]:
    """One row per evidence line of `link`, strongest layers first, collector rows last.

    `dataset` must hold both endpoint incidents (e.g. `AnalysisResult.to_dataset()`);
    `population` (default `dataset`) is what "seen in k / N" counts over, normally the base.
    """
    indicators = {i.id: i for i in dataset.indicators()}
    pop = population or dataset
    rows = []
    for ev in link.evidence:
        inds = [indicators[i] for i in ev.indicator_ids if i in indicators]
        raw = ev.value if ev.value is not None else " | ".join(dict.fromkeys(_indicator_text(i) for i in inds))
        if ev.seen_in:
            seen = ev.seen_in
        elif inds and inds[0].value is not None:
            seen = seen_in(pop, ev.kind, inds[0].value)
        else:
            seen = ""
        note = " ".join(x for x in (STATUS_NOTES.get(ev.status), ev.note) if x)
        rows.append(LedgerRow(
            layer=ev.layer.value,
            kind=ev.kind,
            value=display_value(raw),
            seen_in=seen,
            bits=ev.bits,
            status=STATUS_LABELS[ev.status],
            note=note,
            greyed=ev.status is EvidenceStatus.collector_excluded or ev.layer is Layer.collector,
        ))
    rows.sort(key=lambda r: (r.greyed, -abs(r.bits), r.layer, r.kind))
    return rows


def relation_sentence(link: Link, review_threshold: float = REVIEW_THRESHOLD_BITS) -> str:
    """e.g. 'same template: yes, +5.5 bits (Medium)'."""
    label = RELATION_LABELS[link.relation]
    t = tier(link, review_threshold)
    verdict = "insufficient alone" if t == INSUFFICIENT else "yes"
    positive = sum(1 for e in link.evidence if e.status is EvidenceStatus.shared and e.bits > 0)
    conflicts = sum(1 for e in link.evidence if e.status is EvidenceStatus.conflict)
    extra = f"; {positive} shared layer(s)" + (f", {conflicts} conflict(s)" if conflicts else "")
    return f"{label}: {verdict}, {fmt_bits(link.score_bits)} ({t}{extra})"


def missing_modalities(incident: Incident) -> list[MissingModality]:
    """Modalities missing from every capture of the incident (present anywhere means not missing)."""
    present = {a.type for c in incident.captures for a in c.artefacts}
    seen: dict[str, MissingModality] = {}
    for c in incident.captures:
        for m in c.missing:
            if m.modality not in present:
                seen.setdefault(m.modality.value, m)
    return list(seen.values())


def what_would_change(assignment: Assignment, missing: Sequence[MissingModality]) -> list[str]:
    """Lines saying how far the decision is from changing and which inputs could move it."""
    best = max((c.score_bits for c in assignment.candidates), default=None)
    lines = []
    if best is None:
        lines.append(f"No candidate campaign was scored; evidence capacity {assignment.evidence_capacity_bits:.1f} bits.")
    elif assignment.decision.value == "assign":
        lines.append(f"Assigned with {best:.1f} bits; a conflict on a single-valued layer would reopen it.")
    else:
        gap = ASSIGN_THRESHOLD_BITS - best
        lines.append(
            f"Best candidate {best:.1f} bits of an evidence capacity of {assignment.evidence_capacity_bits:.1f} bits; "
            f"about {max(gap, 0):.1f} more bits would reach assignment (display threshold {ASSIGN_THRESHOLD_BITS:.0f} bits)."
        )
    for m in missing:
        bits, layers = MODALITY_BITS.get(m.modality.value, (None, None))
        name = m.modality.value.upper() if m.modality.value in ("har", "html", "mhtml") else m.modality.value.replace("_", " ")
        if bits is None:
            lines.append(f"{name} missing ({m.reason}).")
        else:
            lines.append(f"{name} missing: up to +{bits:.0f} bits available from the {layers} ({m.reason}).")
    return lines


def feature_rows(incident: Incident) -> dict[str, list[dict]]:
    """Indicators grouped by layer (contract order), each row safe to display."""
    order = [l.value for l in Layer]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for ind in incident.indicators:
        prov = ind.provenance
        where = " · ".join(x for x in (prov.artefact_id, prov.locator, display_value(prov.note, 80) if prov.note else None) if x)
        grouped[ind.layer.value].append({
            "kind": ind.kind,
            "value": display_value(_indicator_text(ind)),
            "level": ind.level.value,
            "observed": ind.observed.value,
            "capture": ind.capture_id,
            "provenance": where,
            "extractor": ind.extractor,
        })
    return {layer: grouped[layer] for layer in order if layer in grouped}


def decision_summary(assignment: Assignment, campaigns: Dataset) -> dict:
    """Decision card data: label, chosen campaign, candidates with bits and tier."""
    labels = {c.id: c.label for c in campaigns.campaigns}
    cands = []
    for c in sorted(assignment.candidates, key=lambda c: -c.score_bits):
        if c.score_bits >= ASSIGN_THRESHOLD_BITS:
            strength = "assign range"
        elif c.score_bits >= REVIEW_THRESHOLD_BITS:
            strength = "review range"
        else:
            strength = INSUFFICIENT
        cands.append({
            "campaign": c.campaign_id,
            "label": truncate(labels.get(c.campaign_id, ""), 80),
            "bits": round(c.score_bits, 2),
            "strength": strength,
            "note": c.note or "",
        })
    return {
        "decision": assignment.decision.value,
        "label": DECISION_LABELS[assignment.decision.value],
        "campaign_id": assignment.campaign_id,
        "campaign_label": truncate(labels.get(assignment.campaign_id or "", ""), 80),
        "evidence_capacity_bits": assignment.evidence_capacity_bits,
        "method": assignment.method,
        "note": assignment.note or "",
        "candidates": cands,
    }


def similar_rows(similar: Iterable, base: Dataset) -> list[dict]:
    """'Similar incidents' table from `AnalysisResult.similar`."""
    sources = {i.id: i.source for i in base.incidents}
    return [
        {
            "incident": s.incident_id,
            "source": sources.get(s.incident_id, ""),
            "their capture": s.capture_id,
            "our capture": s.own_capture_id,
            "bits": None if s.score_bits is None else round(s.score_bits, 2),
            "relation": RELATION_LABELS[Relation(s.relation)],
            "tier": LEVEL_LABELS[LinkLevel(s.level)] if s.score_bits is None or s.score_bits >= REVIEW_THRESHOLD_BITS else INSUFFICIENT,
        }
        for s in similar
    ]


STATUS_ICONS = {"ok": "✅", "skipped": "⏭️", "not_available": "⚪", "error": "❌"}


def stage_rows(stages: Iterable) -> list[dict]:
    return [
        {"stage": s.name, "status": f"{STATUS_ICONS.get(s.status, '')} {s.status}", "ms": s.duration_ms, "note": defang(s.note)}
        for s in stages
    ]
