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

# Site-specific article URL shapes; anything else falls back to the generic rule.
_ARTICLE = {
    "gazeta.pl": re.compile(r"/7,\d+,\d+,[^/]+\.html$"),
    "tvn24.pl": re.compile(r"-ra\d+$"),
    "interia.pl": re.compile(r",nId,\d+$"),
    "polsatnews.pl": re.compile(r"/wiadomosc/\d{4}-\d{2}-\d{2}/[^/]+/?$"),
    "fakt.pl": re.compile(r"/[a-z0-9-]+/[a-z0-9-]+/\w+-\w+$"),
    "money.pl": re.compile(r"-\d{16,}a\.html$"),
    "bankier.pl": re.compile(r"-\d{7,}\.html$"),
}
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


def site_of(host: str) -> str:
    """Last two labels: good enough for the Polish outlets here (no multi-label suffixes)."""
    parts = host.lower().strip(".").split(".")
    return ".".join(parts[-2:])


def article_links(html: str, base_url: str, limit: int = 10) -> list[str]:
    site = site_of(urlsplit(base_url).hostname or "")
    pattern = _ARTICLE.get(site, _GENERIC)
    p = _Links()
    p.feed(html)
    out: list[str] = []
    for href in p.hrefs:
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or site_of(parts.hostname) != site:
            continue
        url = parts._replace(query="").geturl()
        if pattern.search(parts.path) and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def article_rows(html_path: Path, base_url: str, brand: str, limit: int) -> list[str]:
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    return [f"{u}\t{brand}\tarticle\tlinked from {base_url}" for u in article_links(html, base_url, limit)]
