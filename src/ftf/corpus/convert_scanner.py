"""Convert a pre-v1 scanner snapshot (batch 0) into the scanner's v1 per-capture layout.

Batch 0 was collected by an early HACK-20 scanner that wrote one `runs/<run>/incidents-<source>.json`
per source, hash-named creatives under `creatives/`, and `queue.jsonl`. The v1 scanner (on main,
`ftf.scanner`) writes one folder per item with `capture.json` + `ad.json` (+ `creative-<n>.<ext>`),
which `ftf ingest` reads. Rather than keep a legacy reader, this rebuilds each item as an
`AdRecord` from the old incident's indicators and writes it with the v1 code (`to_incident`,
`write_capture`), so ids follow the v1 scheme: `adlib:<ad id>:ad`, `certpl:<registrable domain>:listing`.

Downloaded creatives are copied next to the item (Meta creative links expire) and re-attached by
sha256. Fields the old JSON never held stay null. Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path

from ftf.contract import CONTRACT_VERSION
from ftf.contract.models import Artefact, ArtefactType, Dataset, Incident
from ftf.scanner.records import EXTRACTOR, AdRecord, normalise_domain, to_incident
from ftf.scanner.store import MANIFEST_VERSION, write_capture

# Old indicator provenance locator -> AdRecord field (single-valued).
_FIELDS = {
    "advertiser_id": "advertiser_id",
    "advertiser_name": "advertiser_name",
    "body": "body",
    "title": "title",
    "link_description": "link_description",
    "caption": "display_domain",
    "collation_id": "collation_id",
    "display_format": "display_format",
    "start_date": "start_date",
    "end_date": "end_date",
    "status": "status",
    "query": "keyword",
}
_OLD_SOURCE = {"scanner_meta": "meta", "scanner_google_atc": "google_atc", "scanner_certpl": "certpl"}


def _registrable(domain: str) -> str:
    try:
        from ftf.enrich.normalise import registrable_domain

        return registrable_domain(domain) or domain
    except Exception:  # PSL snapshot unavailable: fall back to the listed host, as the scanner does
        return domain


def record_from_incident(inc: Incident, keyword: str | None = None) -> AdRecord | None:
    """Rebuild the AdRecord an old scanner incident was made from (None if the ad id is missing)."""
    source = _OLD_SOURCE.get(inc.source)
    if source is None:
        return None
    vals: dict[str, list[str]] = defaultdict(list)
    for ind in inc.indicators:
        if ind.value is None:
            continue
        loc = ind.provenance.locator or ""
        if ind.kind in ("advertiser_page_id", "page_id"):
            vals["page_id"].append(ind.value)
        elif ind.kind in ("advertiser_page_name", "page_name"):
            vals["page_name"].append(ind.value)
        elif ind.kind == "landing_url":
            vals["landing_url"].append(ind.value)
        elif ind.kind == "declared_link_url":
            vals["declared_link_url"].append(ind.value)
        elif ind.kind == "landing_domain":
            vals["landing_domain"].append(ind.value)
        elif ind.kind == "creative_url":
            vals["creative_urls"].append(ind.value)
        elif ind.kind == "ad_platform":
            vals["platforms"].append(ind.value)
        elif loc == "ad_id" or ind.kind.endswith("_ad_id") or ind.kind == "cert_register_id":
            vals["ad_id"].append(ind.value)
        elif loc in _FIELDS:
            vals[_FIELDS[loc]].append(ind.value)
    first = {k: v[0] for k, v in vals.items() if v}
    ad_id = first.get("ad_id") or inc.source_ref
    if not ad_id:
        return None
    rec = AdRecord(source=source, ad_id=ad_id)
    for f in _FIELDS.values():
        if f in first:
            setattr(rec, f, first[f])
    for f in ("page_id", "page_name", "landing_url", "declared_link_url"):
        if f in first:
            setattr(rec, f, first[f])
    rec.creative_urls = list(dict.fromkeys(vals.get("creative_urls", [])))
    rec.platforms = sorted(set(vals.get("platforms", [])))
    if keyword and not rec.keyword:
        rec.keyword = keyword
    if source == "certpl":
        domain = normalise_domain(first.get("landing_domain"))
        if not domain:
            return None
        rec.register_id = ad_id
        rec.ad_id = _registrable(domain)
        rec.landing_domain = domain
    elif not rec.landing_url and first.get("landing_domain"):
        rec.landing_domain = first["landing_domain"]
    return rec


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def convert(scanner_dir: Path, out_dir: Path, runs: list[str] | None = None) -> dict:
    """Write v1 capture folders for every item of the old snapshot under `out_dir`."""
    scanner_dir, out_dir = Path(scanner_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(runs) if runs else None
    queue: dict[str, dict] = {}
    for line in (scanner_dir / "queue.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queue.setdefault(row["incident_id"], row)

    written: dict[str, str] = {}  # stable id -> folder (one folder per item, first seen wins)
    by_source: dict[str, int] = defaultdict(int)
    skipped: list[str] = []
    creatives = 0
    for f in sorted(scanner_dir.glob("runs/*/incidents-*.json")):
        run_id = f.parent.name
        if wanted is not None and run_id not in wanted:
            continue
        for old in Dataset.from_json(f.read_bytes()).incidents:
            q = queue.get(old.id, {})
            rec = record_from_incident(old, q.get("keyword"))
            if rec is None:
                skipped.append(old.id)
                continue
            if rec.incident_id in written:
                continue
            folder = rec.folder(run_id)
            arts: list[Artefact] = []
            olds = [
                a
                for c in old.captures
                for a in c.artefacts
                if a.type in (ArtefactType.ad_screenshot, ArtefactType.asset) and a.path
            ]
            for n, a in enumerate(olds):
                src = scanner_dir / a.path
                if not src.is_file():
                    continue
                rel = Path(folder) / f"creative-{n}{src.suffix or '.bin'}"
                (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out_dir / rel)
                arts.append(
                    Artefact(
                        id=f"{rec.capture_ref}:creative-{n}",
                        type=ArtefactType.asset,
                        path=str(rel),
                        sha256=_sha256(src),
                        size=src.stat().st_size,
                    )
                )
                creatives += 1
            captured = old.captures[0].captured_on or run_id[:4] + "-" + run_id[4:6] + "-" + run_id[6:8]
            inc = to_incident(rec, date.fromisoformat(captured), arts)
            queued = q.get("queued_at")
            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "contract_version": CONTRACT_VERSION,
                "tool": EXTRACTOR,
                "capture_id": folder,
                "source": rec.source,
                "ad_id": rec.ad_id,
                "stable_id": rec.incident_id,
                "run_id": run_id,
                "started_at": queued,
                "finished_at": queued,
                "converted_from": f"{f.relative_to(scanner_dir)}#{old.id}",
                "incident": json.loads(inc.model_dump_json()),
            }
            ad = {
                **asdict(rec),
                "key": rec.key,
                "stable_id": rec.incident_id,
                "ad_archive_id": None if rec.source == "certpl" else rec.ad_id,
                "kind": "domain" if rec.source == "certpl" else "ad",
                "landing_domain": rec.effective_landing_domain(),
                "run_id": run_id,
            }
            write_capture(out_dir, folder, manifest, ad)
            written[rec.incident_id] = folder
            by_source[rec.source] += 1
    return {"captures": len(written), "creatives": creatives, "skipped": skipped, "by_source": dict(by_source)}
