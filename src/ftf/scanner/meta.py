"""Meta Ad Library: parse search results and collect them with a no-login headless browser.

Owner decision (HACK-6): no official API (it needs an identity-verified developer account), so
the scanner opens the public Ad Library search page in headless Chromium without logging in,
and reads the ads from the page's own GraphQL responses (`AdLibrarySearchPaginationQuery`) and
from the JSON embedded in the first page. It never replays requests itself, because Meta's
GraphQL `doc_id`s rotate. Meta's terms forbid automated collection without permission; see the
README ("Ad-library scanner") for the terms issue and the production route (official API).

Parsing is pure (no browser) and unit-tested against sanitised fixtures. The collector needs
the optional `playwright` package and a Chromium build (`pip install 'ftf[fetch]'` and
`playwright install chromium`) and runs only on the sandbox VM; the jury image does not include it (the source then reports
`unavailable`).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import urlencode

from ftf.scanner.records import AdRecord, clean_text, is_platform_link, unix_to_date, unwrap_redirect

SEARCH_URL = "https://www.facebook.com/ads/library/"
GRAPHQL_PATH = "/api/graphql/"
SEARCH_QUERY_NAME = "AdLibrarySearchPaginationQuery"
RATE_LIMIT = "Rate limit exceeded"
_SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
_MAX_WALK_DEPTH = 60


def search_url(keyword: str, country: str = "PL", since: Optional[str] = None) -> str:
    params = {
        "active_status": "all",
        "ad_type": "all",
        "country": country,
        "q": keyword,
        "search_type": "keyword_unordered",
        "media_type": "all",
    }
    if since:
        params["start_date[min]"] = since
    return SEARCH_URL + "?" + urlencode(params)


def _json_documents(text: str) -> Iterator[Any]:
    """Yield every JSON value in a GraphQL response body.

    Meta prefixes some bodies with `for (;;);` and streams several JSON documents (one per line)
    in one response; malformed fragments are skipped.
    """
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            nxt = text.find("\n", i)
            if nxt == -1:
                break
            i = nxt + 1
            continue
        yield value
        i = end


def _ad_nodes(value: Any, depth: int = 0) -> Iterator[dict]:
    """Every dict that looks like an ad-library result (has `ad_archive_id` and `snapshot`).

    Walks the whole document instead of a fixed path, so it keeps working when Meta moves the
    results inside the response (search edges, `collated_results`, SSR preload payloads).
    """
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(value, dict):
        if "ad_archive_id" in value and isinstance(value.get("snapshot"), dict):
            yield value
            return
        for v in value.values():
            yield from _ad_nodes(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _ad_nodes(v, depth + 1)


def _text(value: Any) -> Optional[str]:
    """Ad body: a plain string or `{"text": …}`."""
    if isinstance(value, dict):
        value = value.get("text")
    return clean_text(value)


def _str_id(value: Any) -> Optional[str]:
    if isinstance(value, (int, str)) and str(value).strip():
        return str(value).strip()
    return None


def _http_url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


def _creatives(snapshot: dict) -> list[str]:
    urls: list[str] = []

    def take(item: Any) -> None:
        if not isinstance(item, dict):
            return
        for key in ("original_image_url", "resized_image_url", "video_preview_image_url"):
            url = _http_url(item.get(key))
            if url:
                urls.append(url)
                if key == "original_image_url":
                    break  # the resized copy of the same image adds nothing

    for item in snapshot.get("images") or []:
        take(item)
    for item in snapshot.get("videos") or []:
        take(item)
    for card in snapshot.get("cards") or []:
        take(card)
    return list(dict.fromkeys(urls))


def parse_node(node: dict, keyword: Optional[str] = None) -> Optional[AdRecord]:
    ad_id = _str_id(node.get("ad_archive_id"))
    if not ad_id:
        return None
    snap = node.get("snapshot") or {}
    cards = [c for c in snap.get("cards") or [] if isinstance(c, dict)]
    first_card = cards[0] if cards else {}
    declared = [unwrap_redirect(u) for u in [_http_url(snap.get("link_url")), *(_http_url(c.get("link_url")) for c in cards)] if u]
    # Prefer the first link that leaves Meta; fb.me / facebook.com shims are kept only as declared.
    landing = next((u for u in declared if not is_platform_link(u)), None)
    platforms = node.get("publisher_platform") or node.get("publisher_platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    return AdRecord(
        source="meta",
        ad_id=ad_id,
        keyword=keyword,
        page_id=_str_id(node.get("page_id") or snap.get("page_id")),
        page_name=clean_text(node.get("page_name") or snap.get("page_name")),
        body=_text(snap.get("body")) or _text(first_card.get("body")),
        title=clean_text(snap.get("title")) or clean_text(first_card.get("title")),
        link_description=clean_text(snap.get("link_description")) or clean_text(first_card.get("link_description")),
        display_domain=clean_text(snap.get("caption")) or clean_text(first_card.get("caption")),
        landing_url=landing,
        declared_link_url=declared[0] if declared and declared[0] != landing else None,
        creative_urls=_creatives(snap),
        collation_id=_str_id(node.get("collation_id")),
        display_format=clean_text(snap.get("display_format")),
        platforms=sorted({str(p).upper() for p in platforms if isinstance(p, str)}),
        start_date=unix_to_date(node.get("start_date")),
        end_date=unix_to_date(node.get("end_date")),
        status=clean_text(node.get("gated_type")),
    )


def parse_graphql(text: str, keyword: Optional[str] = None) -> list[AdRecord]:
    """Ads in one GraphQL response body (any number of JSON documents)."""
    out: list[AdRecord] = []
    for doc in _json_documents(text):
        for node in _ad_nodes(doc):
            rec = parse_node(node, keyword)
            if rec:
                out.append(rec)
    return out


def graphql_errors(text: str) -> list[str]:
    """Error messages in a GraphQL response body (e.g. "Rate limit exceeded", code 1675004)."""
    out: list[str] = []
    for doc in _json_documents(text):
        if isinstance(doc, dict):
            for err in doc.get("errors") or []:
                if isinstance(err, dict):
                    out.append(f"{err.get('message', '?')} ({err.get('code', '?')})"[:200])
    return out


def parse_ssr_html(html: str, keyword: Optional[str] = None) -> list[AdRecord]:
    """Ads from the JSON blocks embedded in the first page (`<script type="application/json">`).

    The HTML is only scanned as text for those blocks; nothing in it is executed or rendered.
    """
    out: list[AdRecord] = []
    for block in _SCRIPT_JSON.findall(html):
        try:
            doc = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in _ad_nodes(doc):
            rec = parse_node(node, keyword)
            if rec:
                out.append(rec)
    return out


@dataclass
class MetaResult:
    keyword: str
    records: list[AdRecord] = field(default_factory=list)
    status: str = "ok"  # ok | empty | throttled | blocked | error | unavailable | skipped
    evidence: dict = field(default_factory=dict)


def _is_search_response(response) -> bool:
    if GRAPHQL_PATH not in response.url:
        return False
    try:
        post = response.request.post_data or ""
    except Exception:  # binary or missing body
        return False
    return SEARCH_QUERY_NAME in post


def collect(
    keywords: list[str],
    *,
    since: Optional[str],
    country: str = "PL",
    max_per_keyword: int = 50,
    max_total: int = 150,
    delay: float = 5.0,
    max_scrolls: int = 20,
    timeout_s: float = 60.0,
    skip: Optional[set[str]] = None,
) -> list[MetaResult]:
    """Search the Ad Library once per keyword in one headless browser; stop at the caps.

    Caps count only ads whose key is not in `skip` (seen in earlier runs or for an earlier
    keyword); `skip` is updated with every ad returned, so later keywords and runs go deeper.

    Returns one result per keyword with a status and evidence (HTTP status, final URL, title,
    GraphQL calls seen), so throttling or a login wall shows up in the run summary.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [
            MetaResult(k, status="unavailable", evidence={"reason": "playwright not installed; pip install 'ftf[fetch]' && playwright install chromium"})
            for k in keywords
        ]

    skip = skip if skip is not None else set()
    results: list[MetaResult] = []
    total = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(locale="pl-PL", viewport={"width": 1366, "height": 900})
                # Polite and lighter: no images, media or fonts are loaded by the page itself.
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "media", "font"}
                    else route.continue_(),
                )
                for i, keyword in enumerate(keywords):
                    if total >= max_total:
                        results.append(MetaResult(keyword, status="skipped", evidence={"reason": "run cap reached"}))
                        continue
                    if i:
                        time.sleep(delay)
                    cap = min(max_per_keyword, max_total - total)
                    res = _search_one(context, keyword, since, country, cap, delay, max_scrolls, timeout_s, PlaywrightError, skip)
                    skip.update(r.key for r in res.records)
                    total += len(res.records)
                    results.append(res)
            finally:
                browser.close()
    except Exception as exc:  # browser failed to start, crashed, …
        done = {r.keyword for r in results}
        results.extend(
            MetaResult(k, status="error", evidence={"error": f"{type(exc).__name__}: {exc}"[:500]})
            for k in keywords
            if k not in done
        )
    return results


def _search_one(context, keyword, since, country, cap, delay, max_scrolls, timeout_s, PlaywrightError, skip) -> MetaResult:
    res = MetaResult(keyword)
    seen: dict[str, AdRecord] = {}
    pending: list = []
    graphql_calls = {"count": 0, "statuses": []}
    errors: list[str] = []
    page = context.new_page()

    def on_response(response) -> None:
        if _is_search_response(response):
            pending.append(response)

    def drain() -> None:
        while pending:
            response = pending.pop(0)
            graphql_calls["count"] += 1
            graphql_calls["statuses"].append(response.status)
            try:
                body = response.text()
            except PlaywrightError:
                continue
            errors.extend(graphql_errors(body))
            for rec in parse_graphql(body, keyword):
                seen.setdefault(rec.ad_id, rec)

    def fresh() -> list[AdRecord]:
        return [r for r in seen.values() if r.key not in skip]

    page.on("response", on_response)
    try:
        url = search_url(keyword, country, since)
        main = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        page.wait_for_timeout(min(delay, 8.0) * 1000)
        for rec in parse_ssr_html(page.content(), keyword):
            seen.setdefault(rec.ad_id, rec)
        drain()
        stale = 0
        for _ in range(max_scrolls):
            if len(fresh()) >= cap:
                break
            before = len(seen)
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(delay * 1000)
            drain()
            stale = stale + 1 if len(seen) == before else 0
            if stale >= 2 or any(RATE_LIMIT in e for e in errors):
                break  # no more results, or Meta asked us to slow down: stop for this keyword
        final_url = page.url
        title = page.title()
        res.evidence = {
            "http_status": main.status if main else None,
            "final_url": final_url[:300],
            "title": title[:200],
            "graphql_search_calls": graphql_calls["count"],
            "graphql_statuses": sorted(set(graphql_calls["statuses"])),
            "ads_seen": len(seen),
            "ads_new": len(fresh()),
        }
        # The first response is often a 403 JS challenge that the browser then passes, so an
        # error status counts as a block only when no ads arrived.
        walled = any(s in final_url for s in ("/login", "/checkpoint", "__rd_verify"))
        refused = (main is not None and main.status >= 400) or any(s >= 400 for s in graphql_calls["statuses"])
        if walled or (refused and not seen):
            res.status = "blocked"
        elif not seen:
            res.status = "empty"
        if refused and seen:
            res.evidence["challenge_passed"] = True
        if errors:
            res.evidence["graphql_errors"] = sorted(set(errors))[:5]
            if any(RATE_LIMIT in e for e in errors) and res.status != "blocked":
                res.status = "throttled"  # first page kept, pagination refused
    except Exception as exc:  # timeouts, a crashed page: keep the ads parsed so far
        res.status = "error"
        res.evidence = {"error": f"{type(exc).__name__}: {exc}"[:500], "ads_seen": len(seen)}
    finally:
        try:
            page.close()
        except Exception:
            pass
    res.records = fresh()[:cap]
    return res
