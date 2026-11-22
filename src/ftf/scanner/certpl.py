"""CERT Polska warning list (hole.cert.pl): domains newly listed as dangerous.

The list is public and keyless (`domains.json`, ~126k entries with register id, domain and
listing time). A run downloads it once, keeps entries listed since `--since` and not delisted,
optionally keeps only domains containing one of the keywords, and returns the newest first.
Attribution: CERT Polska / NASK, "Lista Ostrzeżeń".
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from ftf.enrich.normalise import registrable_domain
from ftf.scanner.http import Client, FetchError
from ftf.scanner.records import AdRecord, normalise_domain

LIST_URL = "https://hole.cert.pl/domains/v2/domains.json"


def _fold(text: str) -> str:
    """Lower-case ASCII without spaces, so "Placeholder Offer" matches `placeholderoffer.example`."""
    norm = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(ch for ch in norm if ch.isalnum() and ord(ch) < 128).lower()


def parse_list(doc: Any, *, since: Optional[date], keywords: Optional[list[str]] = None) -> list[AdRecord]:
    if not isinstance(doc, list):
        raise ValueError("domains.json is not a list")
    folded = [(k, _fold(k)) for k in keywords or [] if _fold(k)]
    out: list[AdRecord] = []
    for e in doc:
        if not isinstance(e, dict) or e.get("DeleteDate"):
            continue
        rid, dom, ins = e.get("RegisterPositionId"), e.get("DomainAddress"), e.get("InsertDate")
        domain = normalise_domain(dom) if isinstance(dom, str) else None
        if rid is None or not domain or not isinstance(ins, str) or len(ins) < 10:
            continue
        listed = ins[:10]
        if since and listed < since.isoformat():
            continue
        matched = None
        if folded:
            flat = domain.replace(".", "").replace("-", "")
            matched = next((k for k, f in folded if f in flat), None)
            if matched is None:
                continue
        try:
            key = registrable_domain(domain) or domain
        except Exception:  # PSL snapshot unavailable: fall back to the listed host
            key = domain
        out.append(
            AdRecord(
                source="certpl",
                ad_id=key,
                register_id=str(rid),
                keyword=matched,
                landing_domain=domain,
                start_date=listed,
                status="listed",
            )
        )
    # Newest first; one record per registrable domain (its newest listing).
    out.sort(key=lambda r: (r.start_date or "", r.register_id or ""), reverse=True)
    return list({r.ad_id: r for r in reversed(out)}.values())[::-1]


@dataclass
class CertResult:
    keyword: str
    records: list[AdRecord] = field(default_factory=list)
    status: str = "ok"
    evidence: dict = field(default_factory=dict)


def collect(keywords: Optional[list[str]], *, since: Optional[date], client: Client, max_total: int = 150, skip: Optional[set[str]] = None) -> CertResult:
    """One download; `skip` holds already-seen keys so the cap counts only new domains."""
    res = CertResult(",".join(keywords) if keywords else "*")
    try:
        resp = client.request(LIST_URL, headers={"Accept": "application/json"})
        recs = parse_list(json.loads(resp.body), since=since, keywords=keywords)
    except FetchError as exc:
        res.status = "blocked" if exc.status in (403, 429) else "error"
        res.evidence = {"error": str(exc)[:300]}
        return res
    except (ValueError, json.JSONDecodeError) as exc:
        res.status, res.evidence = "error", {"error": f"unparseable list: {exc}"[:300]}
        return res
    fresh = [r for r in recs if not skip or r.key not in skip]
    res.records = fresh[:max_total]
    res.evidence = {"listed_since": len(recs), "new": len(fresh), "kept": len(res.records), "bytes": len(resp.body)}
    if not res.records:
        res.status = "empty"
    return res
