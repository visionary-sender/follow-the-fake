"""Registrable-domain normalisation with the offline Public Suffix List snapshot.

tldextract is pointed at its bundled snapshot (`suffix_list_urls=()`), so no list is downloaded
and nothing is fetched at import or call time. Private suffixes (hosting platforms such as
`x.github.io`) are kept, so each hosted site is its own registrable name.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import tldextract

DEFAULT_TLD_CACHE = Path(
    os.environ.get("TLDEXTRACT_CACHE") or Path.home() / ".cache" / "ftf" / "tldextract"
)

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@lru_cache(maxsize=4)
def _extractor(cache_dir: str) -> tldextract.TLDExtract:
    return tldextract.TLDExtract(
        cache_dir=cache_dir,
        suffix_list_urls=(),
        include_psl_private_domains=True,
    )


def hostname(value: str) -> str | None:
    """Return the lower-case ASCII host of a domain or URL, or None if it is not a host name."""
    text = value.strip().replace("\\", "/")  # browsers treat backslash as a slash
    if not text:
        return None
    if "://" not in text:
        text = "//" + text  # urlsplit only finds a host after a scheme or '//'
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    if host.startswith("*."):
        host = host[2:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    return host


def registrable_domain(value: str, *, tld_cache: Path | None = None) -> str | None:
    """Registrable domain (`example.co.uk`, `victim.github.io`) of a domain or URL.

    Returns None for IP addresses, bare public suffixes and anything that is not a host name.
    """
    host = hostname(value)
    if host is None:
        return None
    extracted = _extractor(str(tld_cache or DEFAULT_TLD_CACHE)).extract_str(host)
    return extracted.top_domain_under_public_suffix or None
