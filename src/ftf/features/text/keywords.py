"""Keywords and topic from a fixed scam lexicon (Polish, with English variants).

A fixed lexicon keeps the output independent of the batch (``extract`` sees one incident, so
corpus TF-IDF would change with the input). Each entry: canonical keyword, topic, stem regex.
"""

from __future__ import annotations

import re
from collections import Counter

_LEXICON: list[tuple[str, str, str]] = [
    # investment
    ("inwestycja", "investment", r"inwest\w*|invest\w*"),
    ("zysk", "investment", r"zysk\w*|zarobi\w*|zarabia\w*|zarobk\w*|profit\w*"),
    ("dochód pasywny", "investment", r"pasywn\w* dochod\w*|dochod\w* pasywn\w*|passive income"),
    ("platforma", "investment", r"platform\w*"),
    ("depozyt", "investment", r"depozyt\w*|wpłat\w*|deposit\w*"),
    ("rejestracja", "investment", r"rejestr\w*|zarejestr\w*|register\w*"),
    ("giełda", "investment", r"giełd\w*|akcj[ie]\w*|trading\w*|trader\w*"),
    ("sztuczna inteligencja", "investment", r"sztuczn\w* inteligencj\w*|(?-i:\bAI\b)|algorytm\w*"),
    ("bank", "investment", r"bank(?!ier)\w*|NBP"),
    ("program rządowy", "investment", r"program\w* (?:rządow\w*|państwow\w*)|rząd\w*"),
    # crypto
    ("kryptowaluty", "crypto", r"krypto\w*|crypto\w*"),
    ("bitcoin", "crypto", r"bitcoin\w*|\bbtc\b"),
    ("blockchain", "crypto", r"blockchain\w*|ethereum\w*"),
    # health
    ("lekarz", "health", r"lekarz\w*|doktor\w*|\bdr\b|kardiolog\w*|profesor\w*"),
    ("lek", "health", r"\blek\b|\bleki\b|\bleku\b|lekarstw\w*|kapsuł\w*|tabletk\w*|suplement\w*"),
    ("cukrzyca", "health", r"cukrzyc\w*|insulin\w*"),
    ("nadciśnienie", "health", r"nadciśnien\w*|ciśnieni\w*"),
    ("stawy", "health", r"staw(?:y|ów|ach|ami|om)\b|kręgosłup\w*"),
    ("odchudzanie", "health", r"odchudz\w*|schudn\w*|otyłoś\w*|kilogram\w*"),
    ("prostata", "health", r"prostat\w*|potencj(?:a|i|ę|ą)\b"),
    # hook
    ("pilne", "hook", r"pilne|pilna|skandal\w*|sensacj\w*|szok\w*"),
    ("tajne śledztwo", "hook", r"śledztw\w*|tajn\w*|ukryt\w* kamer\w*"),
    ("wywiad", "hook", r"wywiad\w*|program\w* na żywo|na antenie"),
    ("zakaz", "hook", r"zakaz\w*|zablokow\w*|usun\w*|ocenzur\w*"),
]

_COMPILED = [(kw, topic, re.compile(rf"(?<!\w)(?:{pat})", re.I)) for kw, topic, pat in _LEXICON]
TOPICS = ("investment", "crypto", "health")


def keywords(text: str, top: int = 10) -> list[str]:
    """Lexicon keywords present in the text, most frequent first."""
    counts = Counter({kw: len(p.findall(text)) for kw, _, p in _COMPILED})
    return [kw for kw, n in counts.most_common(top) if n > 0]


def topic(text: str, min_hits: int = 3) -> str | None:
    """investment / crypto / health by lexicon hits, or None when nothing dominates."""
    hits = Counter()
    for _, t, p in _COMPILED:
        if t in TOPICS:
            hits[t] += len(p.findall(text))
    if not hits:
        return None
    (best, n), *rest = hits.most_common() + [(None, 0)]
    return best if n >= min_hits and n > rest[0][1] else None
