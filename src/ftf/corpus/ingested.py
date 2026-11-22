"""Tag `ftf ingest` output with corpus metadata.

`ftf ingest` (HACK-9) is the canonical reader: it turns the folders of a corpus batch into contract
incidents with the cross-project ids. This module does not build incidents. It writes
`corpus_kind.jsonl` beside the ingested dataset, one row per ingested incident, by joining the
incident with the corpus manifest (`manifest.jsonl` from `ftf corpus build`):

- scanner items (`adlib:<id>`, `certpl:<domain>`) match the manifest by id / registrable domain;
- fetched pages (`fetch:<folder>`) match by fetch folder, from their ids and artefact paths (a
  merged incident holds several fetches of one URL: default, victim profile, other egress).

Fields per row:

- `corpus_kind`: what the item is (docs/corpus.md). `exclude` marks an ingested incident the
  corpus does not vouch for (no manifest item: a capture from a fetcher failure window, or a
  duplicate fetch of a URL already covered). Consumers drop `exclude` rows.
- `scam_label`: how likely the item is a scam, never a ground truth (see `scam_label`). Only
  `listed` and `suspected` should ever be treated as scam-side background; `unknown` is unknown.
- `corpus_flags`: e.g. `wayback_infra` (an Internet Archive snapshot: its network and
  infrastructure indicators describe archive.org, not the outlet; drop that layer).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from ftf.corpus.build import (
    BARE_DOMAIN,
    FETCHED_LANDING,
    MUST_NOT_LINK,
    MUST_NOT_LINK_BLOCKED,
    VICTIM_PROFILE,
    wayback_original,
)

EXCLUDE = "exclude"
_FETCH_ID = re.compile(r"^fetch:([^:]+)")
_TRACKING_PARAM = re.compile(r"^(utm_|fbclid$|gclid$)")


def fetch_id(capture_or_incident_id: str) -> str | None:
    """The fetcher capture folder name inside a `fetch:<folder>[:...]` id."""
    mt = _FETCH_ID.match(capture_or_incident_id)
    return mt.group(1) if mt else None


def _registrable(host: str) -> str:
    try:
        from ftf.enrich.normalise import registrable_domain

        return registrable_domain(host) or host
    except Exception:  # PSL snapshot unavailable, as in the scanner
        return host


def _unwrap(url: str | None) -> str | None:
    """The original page behind an Internet Archive snapshot URL; any other URL unchanged."""
    orig = wayback_original(url) if url else None
    return orig[1] if orig else url


def _domain(url: str | None) -> str | None:
    host = urlsplit(_unwrap(url)).hostname if url else None
    return _registrable(host.lower()) if host else None


def _landing_key(url: str) -> str:
    """Identity of a landing page: host, path and query without tracking parameters."""
    parts = urlsplit(_unwrap(url))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM.match(k.lower())]
    return (parts.hostname or "") + parts.path + ("?" + urlencode(query) if query else "")


def read_lines(path: Path | None) -> set[str]:
    """A plain list (one value per line, `#` comments), lower-cased; empty if no path."""
    if not path:
        return set()
    out = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def _attempts(row: dict) -> list[tuple[str, str | None, str | None, str | None]]:
    """(profile, capture folder, category, final URL) of every fetch of the row's target."""
    out = []
    if row.get("fetch_capture_id"):
        out.append(("default", row["fetch_capture_id"], row.get("fetch_category"), row.get("final_url")))
    if row.get("victim_capture_id"):
        out.append((VICTIM_PROFILE, row["victim_capture_id"], row.get("victim_category"), row.get("victim_final_url")))
    for a in row.get("egress") or []:
        out.append((a.get("profile") or "egress", a.get("capture_id"), a.get("category"), a.get("final_url")))
    return out


def scam_label(rows: list[dict], listed: set[str], legit: set[str], persons: set[str]) -> str:
    """Rule-based label for the items behind one incident (first matching rule wins):

    - `not_scam`: a hand-picked real page;
    - `listed`: a CERT Polska list entry, or a landing domain (default or final URL) on that list;
    - `legit_suspected`: a landing domain on the regulated-broker / brand list (`legit`);
    - `suspected`: an ad found by a person's-name keyword whose landing page was cloaked for the
      default visitor (bot wall / decoy / empty). A dead default fetch is not evidence (the page
      may just be down), so it stays `unknown` even if another profile or egress loaded it;
    - `unknown`: everything else. Never treat `unknown` as scam.
    """
    if any(r["corpus_kind"] in (MUST_NOT_LINK, MUST_NOT_LINK_BLOCKED) for r in rows):
        return "not_scam"
    domains = set()
    for r in rows:
        domains |= {d for d in (_domain(r.get("target_url")), _domain(r.get("final_url"))) if d}
        domains |= {_domain(a[3]) for a in _attempts(r) if a[3]} - {None}
    if any(r.get("source") == "certpl" for r in rows) or domains & listed:
        return "listed"
    if domains & legit:
        return "legit_suspected"
    for r in rows:
        if (r.get("query") or "").strip().lower() not in persons:
            continue
        # Every attempt is checked (no dict keyed by profile, which would let a later attempt hide one).
        if any(p == "default" and c == "cloaked" for p, _, c, _ in _attempts(r)):
            return "suspected"
    return "unknown"


def tag_ingested(
    dataset_path: Path,
    manifest_path: Path,
    out_path: Path,
    legit: set[str] | None = None,
    persons: set[str] | None = None,
    extra_listed: set[str] | None = None,
) -> dict:
    # Domain lists hold hosts too (`www.` or a subdomain): compare registrable domains only.
    legit = {_registrable(d) for d in legit or () if d}
    persons = persons or set()
    extra_listed = {_registrable(d) for d in extra_listed or () if d}
    rows_all: list[dict] = []
    by_fetch: dict[str, list[tuple[dict, str]]] = defaultdict(list)  # folder -> (row, profile)
    by_item: dict[str, list[dict]] = defaultdict(list)  # scanner item key -> rows
    for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows_all.append(row)
        if row["corpus_kind"] == "ad_record":
            by_item[row["incident_id"]].append(row)
        elif row.get("source") == "certpl" and row.get("target_url"):
            # The v1 scanner keys a CERT listing by its registrable domain: certpl:<domain>.
            by_item[f"certpl:{_domain(row['target_url'])}"].append(row)
        for profile, folder, _, _ in _attempts(row):
            if folder:
                by_fetch[folder].append((row, profile))
    listed = {_domain(r["target_url"]) for r in rows_all if r.get("source") == "certpl" and r.get("target_url")}
    listed = {d for d in listed if d} | extra_listed

    ds = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    counts: dict[str, int] = defaultdict(int)
    labels: dict[str, int] = defaultdict(int)
    excluded = 0
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
            item_rows = by_item.get(inc["id"], [])
            hits = [(r, prof) for f in sorted(folders) for r, prof in by_fetch.get(f, [])]
            if item_rows:  # a scanner item itself (ad or CERT listing)
                rows = item_rows
                kind = item_rows[0]["corpus_kind"]  # ad_record, or a CERT item's landing kind (build.py)
                profiles: list[str] = []
            elif hits:
                rows = list({id(r): r for r, _ in hits}.values())
                kinds = {r["corpus_kind"] for r in rows}
                live_folders = {
                    f for r in rows for _, f, cat, _ in _attempts(r) if cat == "live" and f in folders
                }
                if MUST_NOT_LINK in kinds:
                    kind = MUST_NOT_LINK
                elif MUST_NOT_LINK_BLOCKED in kinds:
                    kind = MUST_NOT_LINK_BLOCKED
                else:
                    kind = FETCHED_LANDING if live_folders else BARE_DOMAIN
                profiles = sorted({p for _, p in hits})
            else:
                excluded += 1
                counts[EXCLUDE] += 1
                fh.write(json.dumps({
                    "incident_id": inc["id"], "corpus_kind": EXCLUDE, "scam_label": None, "corpus_flags": [],
                    "fetch_folders": sorted(folders),
                    "reason": "no corpus item: fetcher-failure capture or a duplicate fetch of a covered URL",
                }) + "\n")
                continue
            label = scam_label(rows, listed, legit, persons)
            flags = ["wayback_infra"] if any(r.get("source") == "wayback" for r in rows) else []
            counts[kind] += 1
            labels[label] += 1
            fh.write(json.dumps({
                "incident_id": inc["id"],
                "corpus_kind": kind,
                "scam_label": label,
                "corpus_flags": flags,
                "target_url": rows[0].get("target_url"),
                "profiles": profiles,
                "fetch_folders": sorted(folders),
                "corpus_items": [
                    {"corpus_incident_id": r["incident_id"], "corpus_kind": r["corpus_kind"], "source": r["source"],
                     "query": r.get("query"), "source_ref": r.get("source_ref"),
                     "has_landing_page": r.get("has_landing_page"), "creatives": r.get("creatives")}
                    for r in rows
                ],
            }, ensure_ascii=False) + "\n")
    return {
        "ingested_incidents": len(ds.get("incidents", [])),
        "indicators_by_layer": indicator_layers(ds),
        "tagged": sum(counts.values()) - excluded,
        "excluded": excluded,
        "by_corpus_kind": dict(sorted(counts.items())),
        "by_scam_label": dict(sorted(labels.items())),
        "effective_sample": effective_sample(rows_all, listed, legit, persons),
    }


def effective_sample(rows: list[dict], listed: set[str], legit: set[str], persons: set[str]) -> dict:
    """What the batch really holds, after deduplication by final URL (shared destinations count once)."""
    live_by_label: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["corpus_kind"] in (MUST_NOT_LINK, MUST_NOT_LINK_BLOCKED):
            continue
        finals = {
            _landing_key(f) for _, _, cat, f in _attempts(r) if cat == "live" and f
        }
        if finals:
            live_by_label[scam_label([r], listed, legit, persons)] |= finals
    return {
        "unique_live_landers_by_scam_label": {k: len(v) for k, v in sorted(live_by_label.items())},
        "unique_live_landers": len(set().union(*live_by_label.values())) if live_by_label else 0,
        "ads": sum(1 for r in rows if r["corpus_kind"] == "ad_record" and not r.get("shares_incident")),
        "ads_with_creatives": sum(1 for r in rows if r["corpus_kind"] == "ad_record" and (r.get("creatives") or 0) > 0),
        "must_not_link_pages": sum(1 for r in rows if r["corpus_kind"] == MUST_NOT_LINK),
    }


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
