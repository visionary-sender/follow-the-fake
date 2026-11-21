"""Tag `ftf ingest` output with corpus metadata.

`ftf ingest` (HACK-9) is the canonical reader: it turns the fetcher folders of a corpus batch into
contract incidents with the cross-project ids. This module does not build incidents. It writes
`corpus_kind.jsonl` beside the ingested dataset, one row per ingested incident, by joining the
incident's capture ids with the corpus manifest (`manifest.jsonl` from `ftf corpus build`).

A fetched page can be the landing page of several ads, or a CERT Polska domain, or a hand-picked
real page; the row lists every corpus item that points at it. Its `corpus_kind` describes the page
itself: `must_not_link` / `must_not_link_blocked` for hand-picked pages, else `fetched_landing`
when any of its captures loaded and `bare_domain` when none did.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlsplit
from pathlib import Path

from ftf.corpus.build import BARE_DOMAIN, FETCHED_LANDING, MUST_NOT_LINK, MUST_NOT_LINK_BLOCKED, VICTIM_PROFILE

_FETCH_ID = re.compile(r"^fetch:([^:]+)")


def fetch_id(capture_or_incident_id: str) -> str | None:
    """The fetcher capture folder name inside a `fetch:<folder>[:...]` id."""
    mt = _FETCH_ID.match(capture_or_incident_id)
    return mt.group(1) if mt else None


def tag_ingested(dataset_path: Path, manifest_path: Path, out_path: Path) -> dict:
    by_fetch: dict[str, list[tuple[dict, str]]] = defaultdict(list)  # folder -> (row, profile)
    by_item: dict[str, list[dict]] = defaultdict(list)  # scanner item key -> rows
    for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["corpus_kind"] == "ad_record":
            by_item[row["incident_id"]].append(row)
        elif row.get("source") == "certpl" and row.get("target_url"):
            # The v1 scanner keys a CERT listing by its registrable domain: certpl:<domain>.
            host = urlsplit(row["target_url"]).hostname or ""
            by_item[f"certpl:{_registrable(host)}"].append(row)
        if row.get("fetch_capture_id"):
            by_fetch[row["fetch_capture_id"]].append((row, "default"))
        if row.get("victim_capture_id"):
            by_fetch[row["victim_capture_id"]].append((row, VICTIM_PROFILE))

    ds = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    counts: dict[str, int] = defaultdict(int)
    untagged = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for inc in ds.get("incidents", []):
            folders = {f for f in (fetch_id(inc["id"]), *(fetch_id(c["id"]) for c in inc["captures"])) if f}
            # A merged incident (re-fetches of one URL) names only its first fetch in its ids; the
            # other fetch folders show up in the artefact paths (<fetch dir>/<folder>/<file>).
            for c in inc["captures"]:
                for a in c.get("artefacts", []):
                    parts = (a.get("path") or "").split("/")
                    if len(parts) >= 3 and parts[-2] in by_fetch:
                        folders.add(parts[-2])
            hits = [(r, prof, f) for f in sorted(folders) for r, prof in by_fetch.get(f, [])]
            item_rows = by_item.get(inc["id"], [])
            if item_rows:  # a scanner item itself (ad or CERT listing), ingested from scanner-v1
                kind = item_rows[0]["corpus_kind"] if inc["id"].startswith("adlib:") else "bare_domain"
                counts[kind] += 1
                fh.write(json.dumps({
                    "incident_id": inc["id"],
                    "corpus_kind": kind,
                    "target_url": item_rows[0].get("target_url"),
                    "corpus_items": [
                        {"corpus_incident_id": r["incident_id"], "corpus_kind": r["corpus_kind"], "source": r["source"],
                         "query": r.get("query"), "source_ref": r.get("source_ref"),
                         "has_landing_page": r.get("has_landing_page"), "fetch_capture_id": r.get("fetch_capture_id"),
                         "victim_capture_id": r.get("victim_capture_id")}
                        for r in item_rows
                    ],
                }, ensure_ascii=False) + "\n")
                continue
            if not hits:  # still written, so nothing downstream mistakes it for a tagged item
                untagged += 1
                fh.write(json.dumps({"incident_id": inc["id"], "corpus_kind": None, "fetch_folders": sorted(folders)}) + "\n")
                continue
            kinds = {r["corpus_kind"] for r, _, _ in hits}
            live = any(
                (r.get("fetch_category") if prof == "default" else r.get("victim_category")) == "live"
                for r, prof, _ in hits
            )
            if MUST_NOT_LINK in kinds:
                kind = MUST_NOT_LINK
            elif MUST_NOT_LINK_BLOCKED in kinds:
                kind = MUST_NOT_LINK_BLOCKED
            else:
                kind = FETCHED_LANDING if live else BARE_DOMAIN
            counts[kind] += 1
            fh.write(
                json.dumps(
                    {
                        "incident_id": inc["id"],
                        "corpus_kind": kind,
                        "target_url": hits[0][0].get("target_url"),
                        "profiles": sorted({p for _, p, _ in hits}),
                        "fetch_folders": sorted(folders),
                        "corpus_items": [
                            {
                                "corpus_incident_id": r["incident_id"],
                                "corpus_kind": r["corpus_kind"],
                                "source": r["source"],
                                "query": r.get("query"),
                                "source_ref": r.get("source_ref"),
                                "profile": prof,
                            }
                            for r, prof, _ in hits
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return {
        "ingested_incidents": len(ds.get("incidents", [])),
        "indicators_by_layer": indicator_layers(ds),
        "tagged": sum(counts.values()),
        "untagged": untagged,
        "by_corpus_kind": dict(sorted(counts.items())),
    }



def _registrable(host: str) -> str:
    try:
        from ftf.enrich.normalise import registrable_domain

        return registrable_domain(host) or host
    except Exception:  # PSL snapshot unavailable, as in the scanner
        return host

def indicator_layers(ds: dict) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for inc in ds.get("incidents", []):
        for ind in inc.get("indicators", []):
            counts[ind.get("layer", "?")] += 1
    return dict(sorted(counts.items()))


def check_indicators(summary: dict, ds: dict) -> None:
    """Fail loudly if fetched pages produced no indicators (e.g. a reader without extractors)."""
    fetched = any(c.get("observed") == "live" for i in ds.get("incidents", []) for c in i.get("captures", []))
    if fetched and not summary["indicators_by_layer"]:
        raise SystemExit("corpus tag-ingested: the ingested dataset has fetched pages but 0 indicators; "
                         "re-run ftf ingest from a checkout with the indicator extractors (never --no-indicators)")
