"""Google Ads Transparency Center: keyless search through the site's own RPC endpoints.

The Transparency Center searches advertisers and domains by name, not ad copy. A keyword run
therefore asks `SearchSuggestions` for matching advertisers and domains, then lists each
one's creatives shown in Poland with `SearchCreatives`. Responses use numeric field keys
(protobuf JSON); the field map below was read from live responses and is pinned by tests.
Landing URLs are not published, only the advertiser's domain, so Google records carry a
landing domain but no landing URL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

from ftf.scanner.http import Client, FetchError
from ftf.scanner.records import AdRecord, clean_text, normalise_domain, unix_to_date

BASE = "https://adstransparency.google.com/anji/_/rpc"
REGION_PL = 2616
_URL_IN_TEXT = re.compile(r"https://[^\s\"'<>\\]+")
_CREATIVE_HOSTS = ("tpc.googlesyndication.com", "googleusercontent.com", "ytimg.com", "ggpht.com")
FORMATS = {1: "TEXT", 2: "IMAGE", 3: "VIDEO"}


def _loads(body: bytes) -> Any:
    text = body.decode("utf-8", "replace").lstrip()
    if text.startswith(")]}'"):
        text = text[4:]
    return json.loads(text)


@dataclass
class Suggestions:
    advertisers: list[tuple[str, str]] = field(default_factory=list)  # (AR… id, name)
    domains: list[str] = field(default_factory=list)


def parse_suggestions(doc: Any) -> Suggestions:
    """`SearchSuggestions` → advertisers (`{"1": {"1": name, "2": id, …}}`) and domains (`{"2": {"1": d}}`)."""
    out = Suggestions()
    items = doc.get("1") if isinstance(doc, dict) else None
    local: list[tuple[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        adv = item.get("1")
        if isinstance(adv, dict) and isinstance(adv.get("2"), str) and adv["2"].startswith("AR"):
            pair = (adv["2"], clean_text(adv.get("1")) or "")
            (local if adv.get("3") == "PL" else out.advertisers).append(pair)
        dom = item.get("2")
        if isinstance(dom, dict):
            d = normalise_domain(dom.get("1"))
            if d:
                out.domains.append(d)
    out.advertisers[:0] = local  # Polish advertisers first: the search is limited to Poland
    return out


def _creative_urls(content: Any) -> list[str]:
    """Image and preview URLs anywhere in a creative's content field (HTML snippets included)."""
    urls: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            for u in _URL_IN_TEXT.findall(v):
                u = u.replace("&amp;", "&")
                host = u.split("/", 3)[2].lower() if u.count("/") >= 2 else ""
                if any(host == h or host.endswith("." + h) for h in _CREATIVE_HOSTS):
                    urls.append(u)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(content)
    return list(dict.fromkeys(urls))


def _ts(v: Any) -> Optional[str]:
    return unix_to_date(v.get("1")) if isinstance(v, dict) else None


def parse_creatives(doc: Any, keyword: Optional[str] = None) -> tuple[list[AdRecord], Optional[str]]:
    """`SearchCreatives` → (records, next page token).

    Per creative: "1" advertiser id, "2" creative id, "3" content, "4" format, "6"/"7" first and
    last shown (`{"1": unix seconds}`), "12" advertiser name, "14" advertiser domain.
    """
    records: list[AdRecord] = []
    if not isinstance(doc, dict):
        return records, None
    for c in doc.get("1") or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("2")
        if not isinstance(cid, str) or not cid:
            continue
        fmt = c.get("4")
        records.append(
            AdRecord(
                source="google_atc",
                ad_id=cid,
                keyword=keyword,
                advertiser_id=c.get("1") if isinstance(c.get("1"), str) else None,
                advertiser_name=clean_text(c.get("12")),
                landing_domain=normalise_domain(c.get("14")) if isinstance(c.get("14"), str) else None,
                display_domain=c.get("14") if isinstance(c.get("14"), str) else None,
                creative_urls=_creative_urls(c.get("3")),
                display_format=FORMATS.get(fmt) if isinstance(fmt, int) else None,
                platforms=["GOOGLE"],
                start_date=_ts(c.get("6")),
                end_date=_ts(c.get("7")),
            )
        )
    token = doc.get("2")
    return records, token if isinstance(token, str) and token else None


def _post(client: Client, method: str, req: dict) -> Any:
    body = urlencode({"f.req": json.dumps(req, separators=(",", ":"))}).encode()
    resp = client.request(
        f"{BASE}/{method}?authuser=0",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
        max_bytes=8 * 1024 * 1024,
    )
    return _loads(resp.body)


def creatives_request(
    *, advertiser_id: Optional[str] = None, domain: Optional[str] = None, since: Optional[date] = None,
    until: Optional[date] = None, page_size: int = 40, token: Optional[str] = None,
) -> dict:
    flt: dict[str, Any] = {"8": [REGION_PL]}
    if advertiser_id:
        flt["13"] = {"1": [advertiser_id]}
    if domain:
        flt["12"] = {"1": domain, "2": True}
    if since:
        flt["6"] = int(since.strftime("%Y%m%d"))
        flt["7"] = int((until or date.today()).strftime("%Y%m%d"))
    req: dict[str, Any] = {"2": page_size, "3": flt, "7": {"1": 1, "2": 30, "3": REGION_PL}}
    if token:
        req["4"] = token
    return req


@dataclass
class GoogleResult:
    keyword: str
    records: list[AdRecord] = field(default_factory=list)
    status: str = "ok"
    evidence: dict = field(default_factory=dict)


def collect(
    keywords: list[str],
    *,
    since: Optional[date],
    client: Client,
    max_per_keyword: int = 50,
    max_total: int = 150,
    max_advertisers: int = 3,
    max_pages: int = 3,
    skip: Optional[set[str]] = None,
) -> list[GoogleResult]:
    """Caps count only creatives whose key is not in `skip`; `skip` gets every returned key."""
    skip = skip if skip is not None else set()
    results: list[GoogleResult] = []
    total = 0
    for keyword in keywords:
        res = GoogleResult(keyword)
        results.append(res)
        if total >= max_total:
            res.status, res.evidence = "skipped", {"reason": "run cap reached"}
            continue
        if any(r.status == "blocked" for r in results[:-1]):
            res.status, res.evidence = "skipped", {"reason": "source refused an earlier query (rate limit); not retried this run"}
            continue
        cap = min(max_per_keyword, max_total - total)
        seen: dict[str, AdRecord] = {}
        try:
            sugg = parse_suggestions(_post(client, "SearchService/SearchSuggestions", {"1": keyword, "2": 10, "3": 10}))
            targets = [("advertiser", a) for a, _ in sugg.advertisers[:max_advertisers]]
            targets += [("domain", d) for d in sugg.domains[:max_advertisers]]
            res.evidence = {"advertisers": len(sugg.advertisers), "domains": len(sugg.domains), "queried": len(targets)}
            for kind, value in targets:
                token = None
                for _ in range(max_pages):
                    if len(seen) >= cap:
                        break
                    req = creatives_request(
                        advertiser_id=value if kind == "advertiser" else None,
                        domain=value if kind == "domain" else None,
                        since=since,
                        page_size=40,
                        token=token,
                    )
                    recs, token = parse_creatives(_post(client, "SearchService/SearchCreatives", req), keyword)
                    for r in recs:
                        if since and r.end_date and r.end_date < since.isoformat():
                            continue  # server-side date filter is best effort; enforce it here
                        if r.key not in skip and len(seen) < cap:
                            seen.setdefault(r.ad_id, r)
                    if not token:
                        break
        except FetchError as exc:
            res.status = "blocked" if exc.status in (403, 429) else "error"
            res.evidence["error"] = str(exc)[:300]
        except (ValueError, json.JSONDecodeError) as exc:
            res.status = "error"
            res.evidence["error"] = f"unparseable response: {exc}"[:300]
        res.records = list(seen.values())[:cap]
        skip.update(r.key for r in res.records)
        if res.status == "ok" and not res.records:
            res.status = "empty"
        total += len(res.records)
    return results
