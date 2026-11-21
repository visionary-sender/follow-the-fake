"""Package scanner items, fetcher captures and hand-picked real pages into one contract dataset.

Layout of a corpus directory (`root`), all artefact paths in the output are relative to it:

    scanner/            snapshot of the scanner's data dir: queue.jsonl, runs/, creatives/
    fetch-*/            `ftf fetch` output for queue targets: <capture_id>/capture.json + files
    picked-fetch/       `ftf fetch` output for the hand-picked must-not-link list
    dataset.json        written here: contract Dataset, incidents only
    manifest.jsonl      written here: one flat row per incident
    summary.json        written here: counts per corpus_kind and source, sizes, fetch outcomes

Every incident gets a `corpus_kind` and a `corpus_source` indicator on each capture. Both sit
in the collector layer: they describe how the corpus was gathered, not the scam, so they can
never contribute to a link. See docs/corpus.md.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ftf.contract import models as m
from ftf.corpus.targets import QueueRow, read_picked, read_queue, target_url

EXTRACTOR = "ftf-corpus@0.1.0"

# corpus_kind values (incident level and capture level).
AD_RECORD = "ad_record"
FETCHED_LANDING = "fetched_landing"
BARE_DOMAIN = "bare_domain"
MUST_NOT_LINK = "must_not_link"
KINDS = (AD_RECORD, FETCHED_LANDING, BARE_DOMAIN, MUST_NOT_LINK)

# Sources with an ad behind the queue row; everything else (CERT Polska) is a bare domain list.
AD_SOURCES = {"meta", "google_atc"}
PICKED_SOURCE = "hand_picked_must_not_link"


@dataclass
class FetchedCapture:
    """One `ftf fetch` capture directory, read from its capture.json."""

    rel_dir: str  # relative to the corpus root
    manifest: dict

    @property
    def requested_url(self) -> str:
        return self.manifest["requested_url"]

    @property
    def category(self) -> str:
        return self.manifest.get("category") or "unknown"

    @property
    def outcome(self) -> str:
        return self.manifest.get("outcome") or "unknown"

    @property
    def is_page(self) -> bool:
        """A page was rendered: worth treating as a landing page (live or a cloaked variant)."""
        return self.category == "live"

    @property
    def incident(self) -> m.Incident:
        return m.Incident.model_validate(self.manifest["incident"])


def read_fetch_dirs(root: Path, subdirs: list[str]) -> dict[str, FetchedCapture]:
    """Map requested URL -> capture. A URL fetched twice keeps the later capture."""
    out: dict[str, FetchedCapture] = {}
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        found = []
        for cj in base.glob("*/capture.json"):
            try:
                man = json.loads(cj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(man, dict) or "requested_url" not in man or "incident" not in man:
                continue
            found.append(FetchedCapture(f"{sub}/{cj.parent.name}", man))
        for fc in sorted(found, key=lambda f: f.manifest.get("started_at") or ""):
            out[fc.requested_url] = fc
    return out


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return (s or "x")[:n]


def _tag(capture_id: str, kind: str, value: str, n: int) -> m.Indicator:
    return m.Indicator(
        id=f"{capture_id}/{kind}/{n}",
        capture_id=capture_id,
        layer=m.Layer.collector,
        kind=kind,
        level=m.Level.capture,
        value=value,
        observed=m.Observed.static,
        provenance=m.Provenance(note="corpus builder tag"),
        extractor=EXTRACTOR,
    )


def _tags(capture_id: str, corpus_kind: str, source: str) -> list[m.Indicator]:
    return [_tag(capture_id, "corpus_kind", corpus_kind, 0), _tag(capture_id, "corpus_source", source, 0)]


def lander_capture(
    fc: FetchedCapture, prefix: str, stage: m.Stage, duplicate_of: str | None
) -> tuple[m.Capture, list[m.Indicator]]:
    """Re-key the fetcher's capture under `prefix` (ids must be unique per dataset), with paths
    made relative to the corpus root. Returns the capture and its re-keyed indicators."""
    inc = fc.incident
    src = inc.captures[0]
    cid = f"{prefix}/lander"
    old_art = {}
    arts = []
    for a in src.artefacts:
        new_id = f"{cid}/{a.type.value}"
        if new_id in old_art.values():
            new_id = f"{new_id}-{len(old_art)}"
        old_art[a.id] = new_id
        path = a.path
        if path:
            # Fetcher paths are "<capture_id>/<file>" relative to its --out dir.
            path = f"{fc.rel_dir.rsplit('/', 1)[0]}/{path}"
        arts.append(a.model_copy(update={"id": new_id, "path": path}))
    cap = m.Capture(
        id=cid,
        stage=stage,
        observed=src.observed,
        captured_on=src.captured_on,
        artefacts=arts,
        missing=list(src.missing),
        duplicate_of=duplicate_of,
    )
    inds = []
    for ind in inc.indicators:
        if ind.capture_id != src.id:
            continue
        prov = ind.provenance
        if prov.artefact_id is not None:
            prov = prov.model_copy(update={"artefact_id": old_art.get(prov.artefact_id)})
        inds.append(
            ind.model_copy(
                update={"id": f"{cid}/{ind.kind}/{len(inds)}", "capture_id": cid, "provenance": prov}
            )
        )
    return cap, inds


def _rehome_scanner(inc: m.Incident, scanner: str) -> m.Incident:
    """Scanner artefact paths are relative to its data dir; make them relative to the root."""
    caps = []
    for c in inc.captures:
        arts = [a.model_copy(update={"path": f"{scanner}/{a.path}"}) if a.path else a for a in c.artefacts]
        caps.append(c.model_copy(update={"artefacts": arts}))
    return inc.model_copy(update={"captures": caps})


def _source_label(row: QueueRow) -> str:
    return f"{row.source}:{row.keyword}" if row.keyword else row.source


def _bytes(root: Path, inc: m.Incident) -> int:
    total = 0
    for c in inc.captures:
        for a in c.artefacts:
            if a.size is not None:
                total += a.size
            elif a.path and (root / a.path).is_file():
                total += (root / a.path).stat().st_size
    return total


def build_corpus(
    root: Path,
    scanner: str | None,
    runs: list[str] | None,
    fetch_dirs: list[str],
    picked: Path | None,
    picked_fetch_dirs: list[str],
    name: str = "corpus",
) -> dict:
    root = Path(root)
    incidents: list[m.Incident] = []
    rows_out: list[dict] = []
    canonical_lander: dict[str, str] = {}  # requested URL -> first capture id that holds it

    def attach(prefix: str, url: str | None, fc: FetchedCapture | None, stage: m.Stage, kind: str, label: str):
        if fc is None:
            return None, []
        dup = canonical_lander.get(url) if url else None
        cap, inds = lander_capture(fc, prefix, stage, dup)
        if url and dup is None:
            canonical_lander[url] = cap.id
        return cap, inds + _tags(cap.id, kind, label)

    if scanner:
        fetched = read_fetch_dirs(root, fetch_dirs)
        files: dict[str, dict[str, m.Incident]] = {}
        for row in read_queue(root / scanner / "queue.jsonl", runs):
            if row.incidents_file not in files:
                ds = m.Dataset.from_json((root / scanner / row.incidents_file).read_bytes())
                files[row.incidents_file] = {i.id: i for i in ds.incidents}
            base = files[row.incidents_file].get(row.incident_id)
            if base is None:
                continue
            inc = _rehome_scanner(base, scanner)
            label = _source_label(row)
            url = target_url(row)
            fc = fetched.get(url) if url else None
            is_ad = row.source in AD_SOURCES
            if is_ad:
                kind = AD_RECORD
                first_kind = AD_RECORD
            else:
                kind = FETCHED_LANDING if fc is not None and fc.is_page else BARE_DOMAIN
                first_kind = BARE_DOMAIN  # the list entry itself
            captures = list(inc.captures)
            indicators = list(inc.indicators)
            for c in captures:
                indicators += _tags(c.id, first_kind, label)
            lander_kind = FETCHED_LANDING if fc is not None and fc.is_page else BARE_DOMAIN
            cap, inds = attach(inc.id, url, fc, m.Stage.lander, lander_kind, label)
            if cap is not None:
                captures.append(cap)
                indicators += inds
            inc = inc.model_copy(update={"captures": captures, "indicators": indicators})
            incidents.append(inc)
            rows_out.append(
                {
                    "incident_id": inc.id,
                    "corpus_kind": kind,
                    "source": row.source,
                    "query": row.keyword,
                    "source_label": label,
                    "scanner_key": row.key,
                    "scanner_run": row.run_id,
                    "target_url": url,
                    "fetch_outcome": fc.outcome if fc else None,
                    "fetch_category": fc.category if fc else None,
                    "final_url": fc.manifest.get("final_url") if fc else None,
                    "has_landing_page": bool(fc and fc.is_page),
                    "lander_duplicate_of": cap.duplicate_of if cap is not None else None,
                    "creatives": sum(
                        1 for c in inc.captures for a in c.artefacts if a.type is m.ArtefactType.ad_screenshot
                    ),
                }
            )

    if picked:
        pfetched = read_fetch_dirs(root, picked_fetch_dirs)
        for n, p in enumerate(read_picked(picked)):
            fc = pfetched.get(p.url)
            if fc is None:
                continue
            inc_id = f"mnl-{_slug(p.brand, 24)}-{n:03d}"
            label = f"picked:{p.brand}:{p.page_kind}"
            cap, inds = attach(inc_id, p.url, fc, m.Stage.unknown, MUST_NOT_LINK, label)
            inds.append(_tag(cap.id, "corpus_brand", p.brand, 0))
            inds.append(_tag(cap.id, "corpus_page_kind", p.page_kind, 0))
            incidents.append(m.Incident(id=inc_id, source=PICKED_SOURCE, source_ref=p.url, captures=[cap], indicators=inds))
            rows_out.append(
                {
                    "incident_id": inc_id,
                    "corpus_kind": MUST_NOT_LINK,
                    "source": PICKED_SOURCE,
                    "query": None,
                    "source_label": label,
                    "brand": p.brand,
                    "page_kind": p.page_kind,
                    "target_url": p.url,
                    "fetch_outcome": fc.outcome,
                    "fetch_category": fc.category,
                    "final_url": fc.manifest.get("final_url"),
                    "has_landing_page": fc.is_page,
                    "capture_id": cap.id,
                    "note": p.note,
                }
            )

    ds = m.Dataset(
        name=name,
        description="Background corpus (HACK-18): scanner ads, fetched landing pages, bare domains, must-not-link real pages.",
        incidents=incidents,
    )
    sizes = {i.id: _bytes(root, i) for i in incidents}
    for r in rows_out:
        r["bytes"] = sizes[r["incident_id"]]
    (root / "dataset.json").write_text(ds.to_json(), encoding="utf-8")
    with open(root / "manifest.jsonl", "w", encoding="utf-8") as fh:
        for r in rows_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = summarise(rows_out)
    (root / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def summarise(rows: list[dict]) -> dict:
    by_kind = Counter(r["corpus_kind"] for r in rows)
    by_kind_source = Counter(f"{r['corpus_kind']} | {r['source_label']}" for r in rows)
    outcomes = Counter(f"{r['source']} | {r['fetch_category']} | {r['fetch_outcome']}" for r in rows if r["fetch_outcome"])
    return {
        "incidents": len(rows),
        "by_corpus_kind": dict(sorted(by_kind.items())),
        "by_corpus_kind_and_source": dict(sorted(by_kind_source.items())),
        "ads_with_landing_page": sum(1 for r in rows if r["corpus_kind"] == AD_RECORD and r["has_landing_page"]),
        "fetch_outcomes": dict(sorted(outcomes.items())),
        "not_fetched": sum(1 for r in rows if not r["fetch_outcome"]),
        "bytes_referenced": sum(r["bytes"] for r in rows),
    }
