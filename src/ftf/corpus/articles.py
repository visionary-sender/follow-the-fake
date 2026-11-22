"""Pick real article URLs out of a fetched home page, for the must-not-link list.

The HTML is parsed as inert text with the standard library (no scripts run). Only links on the
same registrable domain as the page are kept, so a hostile page cannot steer the list off-site,
and the result is meant for real outlet home pages only.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

# Article URL path shapes common on Polish news and brand sites (no site names: the outlets come
# from a hand-picked list kept in the bucket). A link is an article if any shape matches its path.
_SHAPES = [
    re.compile(r"/7,\d+,\d+,[^/]+\.html$"),  # comma-separated section/article ids
    re.compile(r"-(?:ra|st)\d{5,}$"),  # slug ending in a prefixed numeric id
    re.compile(r",nId,\d+$"),
    re.compile(r"/wiadomosc/\d{4}-\d{2}-\d{2}/[^/]+/?$"),
    re.compile(r"-\d{16,}a\.html$"),
    re.compile(r"-\d{7,}\.html$"),
]
_GENERIC = re.compile(r"/[^/]*[a-z][^/]*-[^/]*-[^/]*\d[^/]*/?$|/\d{4}/\d{2}/")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v.strip())


# Second-level public suffixes seen among the outlets and brands here (Polish functional domains).
_SUFFIX2 = {"com.pl", "net.pl", "org.pl", "gov.pl", "edu.pl", "info.pl", "biz.pl", "waw.pl", "co.uk"}


def site_of(host: str) -> str:
    """Registrable domain: last two labels, or three under a known two-label suffix (com.pl)."""
    parts = host.lower().strip(".").split(".")
    n = 3 if ".".join(parts[-2:]) in _SUFFIX2 else 2
    return ".".join(parts[-n:])


def article_links(html: str, base_url: str, limit: int = 10) -> list[str]:
    site = site_of(urlsplit(base_url).hostname or "")
    p = _Links()
    p.feed(html)
    out: list[str] = []
    if limit <= 0:
        return out
    for href in p.hrefs:
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or site_of(parts.hostname) != site:
            continue
        query = "&".join(
            q for q in parts.query.split("&") if q and not q.lower().startswith(("utm", "fbclid=", "gclid="))
        )
        url = parts._replace(query=query).geturl()
        path = parts.path
        if (any(p.search(path) for p in _SHAPES) or _GENERIC.search(path)) and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def article_rows(html_path: Path, base_url: str, brand: str, limit: int) -> list[str]:
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    return [f"{u}\t{brand}\tarticle\tlinked from {base_url}" for u in article_links(html, base_url, limit)]
