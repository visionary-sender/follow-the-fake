"""Google Ads Transparency Center via Google's public BigQuery dataset (no scraping, no 429s).

`bigquery-public-data.google_ads_transparency_center.removed_creative_stats` lists creatives
Google removed, per region, with the reason. The source keeps the ones removed for scam-like
reasons (`SCAM_REASONS`, e.g. misrepresentation) and shown in Poland whose last showing is on or after `--since`, newest first. The table has
no ad text, image or landing URL: a record carries the creative and advertiser ids (parsed from
`creative_page_url`), dates and the removal reason. It shares the stable id `adlib:<CR…>` and
the deduplication key with the scraped `google_atc` source, so one creative is queued once.

The query runs through the BigQuery REST API with an OAuth access token from `FTF_BQ_TOKEN`
or `gcloud auth print-access-token`, billed to `--bq-project` (env `FTF_BQ_PROJECT`), with the
job label and `maximumBytesBilled` as a hard cap per query. A dry run scanned
about 3.5 GB, inside the free monthly tier. Without a token or
project the source reports `unavailable`, so the container degrades cleanly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from ftf.scanner.http import Client, FetchError
from ftf.scanner.records import AdRecord, clean_text

API = "https://bigquery.googleapis.com/bigquery/v2"
MAX_BYTES_BILLED = 10 * 1024**3  # 10 GB hard cap per query (one run scans ~3.4 GB)
_PAGE_URL = re.compile(r"(?:/advertiser/(AR[0-9]+))?/creative/(CR[0-9]+)")  # /removed/creative/CR… has no advertiser
# Removal reasons that fit scam ads (counts for Poland, Oct–Nov 2026: Misrepresentation ~10.6k,
# Account suspension ~2.5k, Enabling dishonest behavior ~1.8k, Financial products ~1.3k).
SCAM_REASONS = (
    "Misrepresentation",
    "Account suspension",
    "Abusing the ad network",
    "Enabling dishonest behavior",
    "Financial products and services",
)

QUERY = """
SELECT c.creative_page_url AS url, r.first_shown, r.last_shown,
       d.removal_reason, d.violation_category, d.decision_type
FROM `bigquery-public-data.google_ads_transparency_center.removed_creative_stats` c,
     UNNEST(c.region_stats) r, UNNEST(c.disapproval) d
WHERE r.region_code = @region AND r.last_shown >= @since AND d.removal_reason IN UNNEST(@reasons)
ORDER BY r.last_shown DESC, url
LIMIT @limit
"""


@dataclass
class BqResult:
    keyword: str
    records: list[AdRecord] = field(default_factory=list)
    status: str = "ok"
    evidence: dict = field(default_factory=dict)


def access_token() -> Optional[str]:
    token = os.environ.get("FTF_BQ_TOKEN")
    if token:
        return token.strip()
    gcloud = shutil.which("gcloud")
    if not gcloud:
        return None
    try:
        out = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def parse_rows(resp: dict) -> list[dict]:
    """BigQuery REST rows (`{"f": [{"v": …}]}`) to dicts keyed by the schema field names."""
    names = [f["name"] for f in (resp.get("schema") or {}).get("fields", [])]
    return [{n: cell.get("v") for n, cell in zip(names, row.get("f", []))} for row in resp.get("rows") or []]


def to_records(rows: list[dict]) -> list[AdRecord]:
    out: dict[str, AdRecord] = {}
    for row in rows:
        m = _PAGE_URL.search(row.get("url") or "")
        if not m:
            continue
        advertiser, creative = m.groups()
        reason = "/".join(x for x in (row.get("violation_category"), row.get("removal_reason")) if x)
        out.setdefault(
            creative,
            AdRecord(
                source="google_bq",
                ad_id=creative,
                advertiser_id=advertiser,
                platforms=["GOOGLE"],
                start_date=clean_text(row.get("first_shown")),
                end_date=clean_text(row.get("last_shown")),
                status=f"removed:{reason}" if reason else "removed",
            ),
        )
    return list(out.values())


def collect(*, since: date, client: Client, project: Optional[str], max_total: int = 150, region: str = "PL", skip: Optional[set[str]] = None) -> BqResult:
    res = BqResult("removed_creatives:" + region)
    project = project or os.environ.get("FTF_BQ_PROJECT")
    token = access_token() if project else None
    if not project or not token:
        res.status = "unavailable"
        res.evidence = {"reason": "needs a GCP project (--bq-project / FTF_BQ_PROJECT) and a token (FTF_BQ_TOKEN or gcloud)"}
        return res
    skip = skip or set()
    body = {
        "query": QUERY,
        "useLegacySql": False,
        "parameterMode": "NAMED",
        "queryParameters": [
            {"name": "region", "parameterType": {"type": "STRING"}, "parameterValue": {"value": region}},
            {"name": "since", "parameterType": {"type": "STRING"}, "parameterValue": {"value": since.isoformat()}},
            {"name": "reasons", "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}}, "parameterValue": {"arrayValues": [{"value": r} for r in SCAM_REASONS]}},
            # Over-fetch so that already-seen creatives do not eat the cap.
            {"name": "limit", "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(max_total + len(skip))}},
        ],
        "maximumBytesBilled": str(MAX_BYTES_BILLED),
        "labels": {"ftf-issue": "hack-20"},
        "timeoutMs": 60000,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = json.loads(client.request(f"{API}/projects/{project}/queries", data=json.dumps(body).encode(), headers=headers).body)
        job = resp.get("jobReference") or {}
        for _ in range(20):
            if resp.get("jobComplete"):
                break
            time.sleep(5)
            url = f"{API}/projects/{project}/queries/{job.get('jobId')}?location={job.get('location', '')}&timeoutMs=30000"
            resp = json.loads(client.request(url, headers=headers).body)
        if not resp.get("jobComplete"):
            res.status, res.evidence = "error", {"error": "query did not finish in time", "job_id": job.get("jobId")}
            return res
    except FetchError as exc:
        res.status = "blocked" if exc.status in (403, 429) else "error"
        res.evidence = {"error": str(exc)[:300]}
        return res
    except (ValueError, json.JSONDecodeError) as exc:
        res.status, res.evidence = "error", {"error": f"unparseable response: {exc}"[:300]}
        return res
    recs = [r for r in to_records(parse_rows(resp)) if r.key not in skip]
    res.records = recs[:max_total]
    res.evidence = {
        "job_id": job.get("jobId"),
        "bytes_processed": int(resp.get("totalBytesProcessed") or 0),
        "rows": len(resp.get("rows") or []),
        "new": len(recs),
    }
    if not res.records:
        res.status = "empty"
    return res
