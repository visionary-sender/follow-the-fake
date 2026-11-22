"""Impersonated outlets and brands from text: a small gazetteer matched against OCR, page text
and saved-HTML titles.

A match says the name or logo text appears, not that the page is genuine or fake; the linking
engine decides how much a shared impersonated brand means. Patterns are case-insensitive
unless the entry says otherwise; short or ambiguous names (WP, ING, PKO, FAKT, NBP, ZUS) match
case-sensitively or only in their domain form.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class Brand:
    name: str
    category: str  # news | tv | bank | company | public
    patterns: tuple[str, ...]  # case-insensitive regexes
    exact: tuple[str, ...] = ()  # case-sensitive regexes


def _b(word: str) -> str:
    return rf"(?<!\w){word}(?!\w)"


BRANDS: tuple[Brand, ...] = (
    # news portals and papers
    Brand("Onet", "news", (_b(r"onet(?:\.pl)?"), _b("onet24"))),
    Brand("Wirtualna Polska", "news", (_b(r"wp\.pl"), r"wirtualn\w* polsk\w*"), (_b("WP"),)),
    Brand("Gazeta.pl", "news", (_b(r"gazeta\.pl"), _b(r"next\.gazeta\.pl"))),
    Brand("Gazeta Wyborcza", "news", (r"gazet\w* wyborcz\w*", _b(r"wyborcza\.pl"))),
    Brand("Fakt", "news", (_b(r"fakt\.pl"), _b("fakt24")), (_b("FAKT"),)),
    Brand("Super Express", "news", (r"super ?express", _b(r"se\.pl"))),
    Brand("Interia", "news", (_b(r"interia(?:\.pl)?"),)),
    Brand("Money.pl", "news", (_b(r"money\.pl"),)),
    Brand("Bankier.pl", "news", (_b(r"bankier(?:\.pl)?"),)),
    Brand("Business Insider", "news", (r"business ?insider",)),
    Brand("Rzeczpospolita", "news", (_b(r"rzeczpospolit\w+"), _b(r"rp\.pl"))),
    Brand("Forbes", "news", (_b("forbes"),)),
    Brand("Pudelek", "news", (_b("pudelek"),)),
    Brand("naTemat", "news", (_b(r"natemat(?:\.pl)?"),)),
    Brand("Kanał Zero", "news", (r"kana(?:ł|l) ?zero",)),
    Brand("Wprost", "news", (_b(r"wprost(?:\.pl)?"),), ()),
    Brand("Newsweek", "news", (_b("newsweek"),)),
    Brand("Polityka", "news", (_b(r"polityka\.pl"),)),
    # TV and radio
    Brand("TVN24", "tv", (_b(r"tvn ?24(?:\.pl)?"),)),
    Brand("TVN", "tv", (r"fakty tvn",), (_b("TVN"),)),
    Brand("Polsat News", "tv", (r"polsat ?news",)),
    Brand("Polsat", "tv", (_b(r"polsat(?:\.pl)?"), r"wydarzenia polsat")),
    Brand("TVP", "tv", (r"tvp ?info", _b(r"tvp\.pl"), r"wiadomo(?:ś|s)ci tvp"), (_b("TVP"),)),
    Brand("Radio ZET", "tv", (r"radio ?zet",)),
    Brand("RMF FM", "tv", (r"rmf ?fm", _b(r"rmf24"))),
    # banks and finance
    Brand("PKO Bank Polski", "bank", (r"pko ?bp", r"pko bank polski", _b(r"pkobp\.pl"), r"ipko"), (_b("PKO"),)),
    Brand("Bank Pekao", "bank", (_b("pekao"),)),
    Brand("mBank", "bank", (_b(r"mbank(?:\.pl)?"),)),
    Brand("ING Bank Śląski", "bank", (r"ing bank", _b(r"ing\.pl")), (_b("ING"),)),
    Brand("Santander", "bank", (_b("santander"),)),
    Brand("BNP Paribas", "bank", (r"bnp ?paribas",)),
    Brand("Millennium", "bank", (r"bank millennium", r"millennium bank")),
    Brand("Alior Bank", "bank", (r"alior ?bank",)),
    Brand("Credit Agricole", "bank", (r"cr(?:é|e)dit agricole",)),
    Brand("Revolut", "bank", (_b("revolut"),)),
    Brand("Narodowy Bank Polski", "public", (r"narodow\w* bank\w* polsk\w*",), (_b("NBP"),)),
    Brand("GPW", "company", (r"gie(?:ł|l)d\w* papier(?:ó|o)w warto(?:ś|s)ciowych",), (_b("GPW"),)),
    # companies
    Brand("InPost", "company", (_b("inpost"), r"paczkomat")),
    Brand("Orlen", "company", (_b(r"(?:pkn )?orlen"),)),
    Brand("PGE", "company", (r"polska grupa energetyczna",), (_b("PGE"),)),
    Brand("PZU", "company", (), (_b("PZU"),)),
    Brand("KGHM", "company", (_b("kghm"),)),
    Brand("Allegro", "company", (_b(r"allegro(?:\.pl)?"),)),
    Brand("Baltic Pipe", "company", (r"baltic ?pipe",)),
    Brand("Biedronka", "company", (_b(r"biedronk\w*"),)),
    Brand("Lidl", "company", (_b("lidl"),)),
    Brand("Tesla", "company", (_b("tesla"),)),
    # public institutions
    Brand("ZUS", "public", (r"zak(?:ł|l)ad ubezpiecze(?:ń|n) spo(?:ł|l)ecznych",), (_b("ZUS"),)),
    Brand("NFZ", "public", (r"narodow\w* fundusz\w* zdrowia",), (_b("NFZ"),)),
    Brand("Ministerstwo Zdrowia", "public", (r"ministerstw\w* zdrowia",)),
    Brand("Ministerstwo Finansów", "public", (r"ministerstw\w* finans(?:ó|o)w",)),
    Brand("KNF", "public", (r"komisj\w* nadzoru finansowego",), (_b("KNF"),)),
)

_COMPILED = [
    (
        b,
        [re.compile(p, re.IGNORECASE) for p in b.patterns] + [re.compile(p) for p in b.exact],
    )
    for b in BRANDS
]


def match_brands(text: str) -> list[tuple[Brand, str]]:
    """Brands named in a text, each once, with the first matched span."""
    out = []
    for brand, regs in _COMPILED:
        for r in regs:
            m = r.search(text)
            if m:
                out.append((brand, m.group(0)))
                break
    # A more specific brand subsumes its parent ("TVN24" over "TVN", "Polsat News" over "Polsat").
    names = {b.name for b, _ in out}
    parents = {"TVN24": "TVN", "Polsat News": "Polsat"}
    drop = {parents[n] for n in names if n in parents}
    return [(b, s) for b, s in out if b.name not in drop]


_TITLE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_OG = re.compile(
    rb"<meta[^>]+(?:property|name)\s*=\s*[\"'](?:og:site_name|og:title|application-name)[\"'][^>]*>",
    re.IGNORECASE,
)
_CONTENT = re.compile(rb"content\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)


def html_title_text(path: Path, limit: int = 512 * 1024) -> Optional[str]:
    """``<title>`` and og:site_name/og:title of a saved page, read as inert bytes (no parser,
    nothing executed). Only the first ``limit`` bytes are read."""
    with open(path, "rb") as f:
        raw = f.read(limit)
    parts: list[bytes] = []
    m = _TITLE.search(raw)
    if m:
        parts.append(m.group(1))
    for tag in _OG.findall(raw):
        c = _CONTENT.search(tag)
        if c:
            parts.append(c.group(1))
    if not parts:
        return None
    text = " | ".join(_decode(p) for p in parts)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:500] or None


def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1250", errors="replace")


def brands_in(texts: Iterable[tuple[str, str]]) -> dict[str, tuple[Brand, list[str]]]:
    """Merge brand matches over (source, text) pairs: name -> (brand, ["source:match", ...])."""
    found: dict[str, tuple[Brand, list[str]]] = {}
    for source, text in texts:
        for brand, span in match_brands(text):
            entry = found.setdefault(brand.name, (brand, []))
            tag = f"{source}:{span.strip()[:40]}"
            if tag not in entry[1]:
                entry[1].append(tag)
    return found
