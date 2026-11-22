"""`ftf export`: the linked dataset as flat tables, a validated copy and a campaign-pattern report.

Input: a contract `Dataset` that carries links and campaigns (the output of `ftf link` or
`ftf analyze --out`). Output folder, every file rebuilt from scratch:

- `incidents.csv`, `captures.csv`, `indicators.csv`, `links.csv`, `evidence.csv`, `campaigns.csv`:
  one row per contract entity, ids as in the contract, sorted by id. `evidence.csv` has one row per
  ledger row of every link, so each link's score can be audited line by line.
- `dataset.json`: the validated contract document, re-serialised.
- `campaign_patterns.md`: one generated section per campaign (size, date span, the relations that
  hold it, stages, missing modalities, top shared indicators by bits).

The CSV and JSON files keep indicator values as they are, so analysts can match them. The Markdown
report defangs values (`hxxp`, `[.]`) and escapes table cells, because scam URLs must never be
clickable. Outputs built from organiser data belong in the private bucket, never in git.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from ftf.app import ledger as ledger_view
from ftf.app import relations as rel
from ftf.contract import Assignment, Campaign, Capture, Dataset, EvidenceStatus, Relation, linkable

TABLE_FILES = ("incidents", "captures", "indicators", "links", "evidence", "campaigns")
REPORT_FILE = "campaign_patterns.md"
DATASET_FILE = "dataset.json"
TOP_INDICATORS = 5
SEP = ";"


def export(ds: Dataset, out: Path) -> dict[str, int]:
    """Write every export file into `out`; returns the rows (or campaign sections) written per file."""
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "incidents": _incident_rows(ds),
        "captures": _capture_rows(ds),
        "indicators": _indicator_rows(ds),
        "links": _link_rows(ds),
        "evidence": _evidence_rows(ds),
        "campaigns": _campaign_rows(ds),
    }
    counts: dict[str, int] = {}
    for name in TABLE_FILES:
        rows = tables[name]
        _write_csv(out / f"{name}.csv", rows)
        counts[f"{name}.csv"] = len(rows)
    (out / DATASET_FILE).write_text(ds.to_json() + "\n", encoding="utf-8")
    report = campaign_report(ds)
    (out / REPORT_FILE).write_text(report, encoding="utf-8")
    counts[REPORT_FILE] = report.count("\n## ")
    return counts


# --- flat tables -------------------------------------------------------------------------------


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _assignments(ds: Dataset) -> dict[str, Assignment]:
    return {a.incident_id: a for a in ds.assignments}


def _incident_rows(ds: Dataset) -> list[dict]:
    assigned = _assignments(ds)
    rows = []
    for inc in sorted(ds.incidents, key=lambda i: i.id):
        a = assigned.get(inc.id)
        rows.append({
            "incident_id": inc.id,
            "source": inc.source,
            "source_ref": inc.source_ref or "",
            "n_captures": len(inc.captures),
            "n_indicators": len(inc.indicators),
            "n_linkable_indicators": sum(1 for i in inc.indicators if linkable(i)),
            "decision": a.decision.value if a else "",
            "campaign_id": (a.campaign_id or "") if a else "",
            "evidence_capacity_bits": f"{a.evidence_capacity_bits:g}" if a else "",
        })
    return rows


def _capture_rows(ds: Dataset) -> list[dict]:
    rows = []
    for inc in sorted(ds.incidents, key=lambda i: i.id):
        for cap in sorted(inc.captures, key=lambda c: c.id):
            rows.append({
                "capture_id": cap.id,
                "incident_id": inc.id,
                "stage": cap.stage.value,
                "observed": cap.observed.value,
                "captured_on": cap.captured_on or "",
                "duplicate_of": cap.duplicate_of or "",
                "artefact_types": SEP.join(sorted({a.type.value for a in cap.artefacts})),
                "missing_modalities": SEP.join(sorted(
                    f"{m.modality.value}: {m.reason}" for m in cap.missing
                )),
            })
    return rows


def _indicator_rows(ds: Dataset) -> list[dict]:
    capture_incident = {c.id: i.id for i in ds.incidents for c in i.captures}
    rows = []
    for ind in sorted(ds.indicators(), key=lambda x: x.id):
        rows.append({
            "indicator_id": ind.id,
            "capture_id": ind.capture_id,
            "incident_id": capture_incident.get(ind.capture_id, ""),
            "layer": ind.layer.value,
            "kind": ind.kind,
            "level": ind.level.value,
            "value": ind.value if ind.value is not None else "",
            "vector_key": ind.vector_ref.key if ind.vector_ref else "",
            "observed": ind.observed.value,
            "linkable": "yes" if linkable(ind) else "no",
            "extractor": ind.extractor,
        })
    return rows


def _link_rows(ds: Dataset) -> list[dict]:
    rows = []
    for link in sorted(ds.links, key=lambda l: l.id):
        rows.append({
            "link_id": link.id,
            "a": link.a,
            "b": link.b,
            "relation": link.relation.value,
            "level": link.level.value,
            "cert_tier": rel.LEVEL_LABEL.get(link.level, ""),
            "score_bits": "" if link.score_bits is None else f"{link.score_bits:g}",
            "method": link.method,
            "n_evidence": len(link.evidence),
            "n_shared": sum(1 for r in link.evidence if r.status is EvidenceStatus.shared),
        })
    return rows


def _evidence_rows(ds: Dataset) -> list[dict]:
    rows = []
    for link in sorted(ds.links, key=lambda l: l.id):
        for n, row in enumerate(link.evidence, start=1):
            rows.append({
                "link_id": link.id,
                "row": n,
                "relation": link.relation.value,
                "layer": row.layer.value,
                "kind": row.kind,
                "status": row.status.value,
                "value": row.value or "",
                "bits": "" if row.bits is None else f"{row.bits:g}",
                "seen_in": row.seen_in or "",
                "indicator_ids": SEP.join(row.indicator_ids),
                "note": row.note or "",
            })
    return rows


def _campaign_rows(ds: Dataset) -> list[dict]:
    summary = {s.campaign_id: s for s in rel.campaign_summary(ds, top=TOP_INDICATORS)}
    rows = []
    for c in sorted(ds.campaigns, key=lambda c: (c.relation.value, c.id)):
        s = summary.get(c.id)
        rows.append({
            "campaign_id": c.id,
            "label": c.label,
            "relation": c.relation.value,
            "n_captures": len(c.capture_ids),
            "n_incidents": s.n_incidents if s else "",
            "n_links": len(c.link_ids),
            "first_seen": (s.first_seen or "") if s else "",
            "last_seen": (s.last_seen or "") if s else "",
            "capture_ids": SEP.join(c.capture_ids),
            "link_ids": SEP.join(c.link_ids),
        })
    return rows


# --- campaign patterns report ------------------------------------------------------------------


def campaign_report(ds: Dataset) -> str:
    """Markdown report: an overview table, then one generated section per campaign."""
    summary = {s.campaign_id: s for s in rel.campaign_summary(ds, top=TOP_INDICATORS)}
    capture_index = _capture_index(ds)
    campaigns = sorted(ds.campaigns, key=lambda c: (list(Relation).index(c.relation), c.id))
    lines = [
        "# Campaign patterns",
        "",
        "Generated by `ftf export` from the linked dataset. One section per campaign. Values are",
        "defanged (`hxxp`, `[.]`) and must never be made clickable. Bits are the evidence ledger's",
        "log2 likelihood ratios (see `docs/methodology.md`). A campaign is one partition of one",
        "relation, so the same capture can appear in campaigns of several relations.",
        "",
        "| Campaign | Relation | Captures | Incidents | Date span |",
        "|---|---|---:|---:|---|",
    ]
    for c in campaigns:
        s = summary.get(c.id)
        lines.append("| " + " | ".join([
            _cell(c.id), _cell(rel.RELATION_LABEL[c.relation]), str(len(c.capture_ids)),
            str(s.n_incidents if s else ""), _cell(_span(s)),
        ]) + " |")
    if not campaigns:
        lines.append("| _no campaigns in this dataset_ | | | | |")
    for c in campaigns:
        lines += _campaign_section(c, summary.get(c.id), capture_index, campaigns)
    return "\n".join(lines) + "\n"


def _capture_index(ds: Dataset) -> dict[str, Capture]:
    return {cap.id: cap for inc in ds.incidents for cap in inc.captures}


def _span(s: rel.CampaignSummary | None) -> str:
    if s is None or s.first_seen is None:
        return "not dated"
    if s.first_seen == s.last_seen:
        return s.first_seen
    return f"{s.first_seen} to {s.last_seen}"


def _campaign_section(
    c: Campaign,
    s: rel.CampaignSummary | None,
    captures: dict[str, Capture],
    campaigns: Iterable[Campaign],
) -> list[str]:
    members = [captures[x] for x in c.capture_ids if x in captures]
    stages = Counter(cap.stage.value for cap in members)
    missing: dict[str, int] = defaultdict(int)
    for cap in members:
        for m in {m.modality.value for m in cap.missing}:
            missing[m] += 1
    holds = _holding_relations(c, campaigns)
    lines = [
        "",
        f"## {_cell(c.label or c.id)}",
        "",
        f"- Campaign id: `{_cell(c.id)}`",
        f"- Relation of this partition: {rel.RELATION_LABEL[c.relation]}",
        "- Relations under which all its captures sit together: " + ", ".join(rel.RELATION_LABEL[r] for r in holds),
        f"- Size: {len(c.capture_ids)} captures in {s.n_incidents if s else '?'} incidents, "
        f"{len(c.link_ids)} links",
        f"- Date span (capture day): {_span(s)}",
        "- Stages seen: " + (", ".join(f"{k} {v}" for k, v in sorted(stages.items())) or "none"),
        "- Missing modalities: " + (
            ", ".join(f"{k} ({v} capture{'s' if v != 1 else ''})" for k, v in sorted(missing.items()))
            if missing else "none"
        ),
        "- Captures: " + (", ".join(f"`{_cell(x)}`" for x in c.capture_ids) or "none"),
        "",
    ]
    tops = tuple(t for t in s.top_indicators if t.max_bits > 0) if s else ()
    if not tops:
        lines.append("No shared indicator carries bits in this campaign.")
        return lines
    lines += [
        "Top shared indicators by bits (missing and collector rows never count):",
        "",
        "| Layer | Kind | Value | Links | Max bits |",
        "|---|---|---|---:|---:|",
    ]
    for t in tops:
        value = "(not recorded)" if t.value is None else ledger_view.display_value(t.value)
        lines.append(f"| {_cell(t.layer)} | {_cell(t.kind)} | {_cell(value)} | {t.links} | {t.max_bits:g} |")
    return lines


def _holding_relations(c: Campaign, campaigns: Iterable[Campaign]) -> list[Relation]:
    """Relations R for which some campaign of relation R contains every capture of `c`."""
    mine = set(c.capture_ids)
    holds = []
    for r in Relation:
        if any(o.relation is r and mine <= set(o.capture_ids) for o in campaigns):
            holds.append(r)
    return holds


def _cell(text: object) -> str:
    """Defang and escape a value for one Markdown table cell, heading or inline code span."""
    s = rel.defang("" if text is None else str(text))
    return s.replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("`", "'")
