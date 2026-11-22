"""Page URLs of a HAR file, read as JSON data.

The domains `ftf enrich-domain` looks up come from `ftf.indicators.domains.registered_domains`
(HACK-52); this module keeps only the HAR page-URL reader that the pipeline stages use. HAR data
is parsed as JSON only: nothing in it is executed, imported, rendered or fetched.
"""

from __future__ import annotations

from typing import Any


def page_urls(har: Any) -> list[str]:
    """Page URLs of one HAR: the `pages[].title` URLs, else the first request URL.

    The first request is not always the page: a player or CDN can load before the page
    document, so the page list is preferred when it holds URLs.
    """
    try:
        log = har["log"]
    except (KeyError, TypeError):
        return []
    pages = log.get("pages") if isinstance(log, dict) else None
    if not isinstance(pages, list):
        pages = []
    titles = [p.get("title") for p in pages if isinstance(p, dict)]
    urls = [t for t in titles if isinstance(t, str) and t.lower().startswith(("http://", "https://"))]
    if urls:
        return urls
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return []
    documents = [e for e in entries if isinstance(e, dict) and e.get("_resourceType") == "document"]
    chosen = documents[0] if documents else (entries[0] if entries and isinstance(entries[0], dict) else None)
    request = chosen.get("request") if isinstance(chosen, dict) else None
    url = request.get("url") if isinstance(request, dict) else None
    return [url] if isinstance(url, str) else []
