"""Name-masked text: the same story told about a different celebrity should mask to the same text.

Replaces URLs, outlet names, person names (gazetteer surnames with Polish inflection, plus any
pair of capitalised words), prices, dates and numbers with placeholders. Also reports which
people were named, for ``person_mention``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ftf.features.people import PEOPLE

# Polish case endings appended to a stem (nouns and -ski/-cki adjectival surnames).
_SUFFIX = r"(?:a|e|ę|i|y|o|u|ą|em|om|ie|im|ym|ej|ego|emu|iej|iego|iemu|iem|owi|ach|ami)?"
_UPPER = "A-ZĄĆĘŁŃÓŚŹŻ"
_LOWER = "a-ząćęłńóśźż"

# Common Polish first names (nominative); a capitalised pair starting with one is a person.
FIRST_NAMES = (
    "Adam Agnieszka Aleksander Aleksandra Andrzej Anna Barbara Bartosz Beata Bogdan Cezary "
    "Damian Daniel Dariusz Dominika Dorota Elżbieta Ewa Grzegorz Hanna Ireneusz Iwona Jacek "
    "Jakub Jan Janusz Jarosław Jerzy Joanna Józef Julia Justyna Kamil Karol Katarzyna Kazimierz "
    "Krystyna Krzysztof Kuba Leszek Łukasz Magda Magdalena Maciej Małgorzata Marcin Marek Maria "
    "Mariusz Marta Martyna Mateusz Michał Monika Natalia Paweł Piotr Rafał Robert Ryszard "
    "Sebastian Sławomir Stanisław Szymon Tadeusz Tomasz Wojciech Zbigniew Zofia Zygmunt"
).split()

# News outlets and programmes impersonated as article chrome. Case-sensitive on purpose
# ("Fakt" the tabloid, "fakt" the word).
OUTLETS = [
    "Gazeta Wyborcza", "Gazeta.pl", "Wirtualna Polska", "Onet.pl", "Onet", "WP.pl", "Interia",
    "TVN24", "TVN", "Polsat News", "Polsat", "TVP Info", "TVP", "RMF FM", "Radio ZET",
    "Super Express", "Fakt", "Business Insider", "Forbes", "Rzeczpospolita", "Money.pl",
    "money.pl", "Bankier.pl", "Puls Biznesu", "Newsweek", "Kanał Zero", "Fakty po Faktach",
    "Fakty", "Wydarzenia", "Dzień Dobry TVN", "Pudelek", "Pomponik", "naTemat",
]

_MONTHS = (
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września października "
    "listopada grudnia styczeń luty marzec kwiecień maj czerwiec lipiec sierpień wrzesień "
    "październik listopad grudzień"
).split()


def _stem(word: str) -> str:
    return re.sub(r"[aeiouyąę]$", "", word) if len(word) >= 3 else word


def _full_pattern(name: str) -> re.Pattern:
    """First name and surname in any grammatical case ("Roberta Lewandowskiego") or letter case."""
    first, last = name.split()[0], name.split()[-1]
    return re.compile(
        rf"(?<!\w){re.escape(_stem(first))}{_SUFFIX}\s+{re.escape(_stem(last))}{_SUFFIX}(?!\w)",
        re.I,  # all-caps headlines; a bare surname stays case-sensitive ("Lis" vs "lis")
    )


def _surname_pattern(stem: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(stem)}{_SUFFIX}(?!\w)")


_PEOPLE = [(name, _full_pattern(name)) for name in PEOPLE]
# Surname alone: credited to a person only when no other gazetteer entry shares the stem
# ("Lewandowski" / "Lewandowska" both stem to "Lewandowsk", so a bare surname stays anonymous).
_BY_STEM: dict[str, list[str]] = {}
for _name in PEOPLE:
    _BY_STEM.setdefault(_stem(_name.split()[-1]), []).append(_name)
_SURNAMES = [
    (names[0] if len(names) == 1 else None, _surname_pattern(stem))
    for stem, names in sorted(_BY_STEM.items(), key=lambda kv: -len(kv[0]))
]
_FIRST = re.compile(
    "(?:" + "|".join(sorted({re.escape(_stem(n)) for n in FIRST_NAMES}, key=len, reverse=True)) + ")" + _SUFFIX
)
_URL = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:pl|com|net|org|live|click|top|info|online|site|xyz|eu)\b(?:/\S*)?", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_OUTLET = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(o) for o in sorted(OUTLETS, key=len, reverse=True)) + r")(?!\w)")
_NUMBER = r"\d+(?:[  .,]\d{3})*(?:[.,]\d+)?"
_PRICE = re.compile(
    rf"(?:[$€£]\s?{_NUMBER}|{_NUMBER}\s?(?:zł|złotych|zlotych|PLN|pln|EUR|euro|€|USD|\$|dolarów|tys\.? zł|mln zł))"
)
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{1,2}:\d{2}\b|"
    rf"\b\d{{1,2}}\s+(?:{'|'.join(_MONTHS)})(?:\s+\d{{4}})?\b",
    re.I,
)
_NUM = re.compile(_NUMBER)
_CAP = rf"[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)?"
_BIGRAM = re.compile(rf"(?<!\w)({_CAP})\s+({_CAP})(?!\w)")


@dataclass
class Masked:
    text: str
    people: list[str] = field(default_factory=list)  # gazetteer names, canonical form
    other_names: list[str] = field(default_factory=list)  # first-name + surname pairs as written


def mask(text: str) -> Masked:
    """Mask names, outlets, prices, dates, numbers and URLs; collect the people named."""
    people: list[str] = []
    other: list[str] = []
    t = _EMAIL.sub("<URL>", text)
    t = _OUTLET.sub("<OUTLET>", t)  # before URLs, so "Onet.pl" and "Onet" mask alike
    t = _URL.sub("<URL>", t)
    for name, pat in _PEOPLE:
        t, n = pat.subn("<PERSON>", t)
        if n:
            people.append(name)
    for name, pat in _SURNAMES:
        t, n = pat.subn("<PERSON>", t)
        if n and name and name not in people:
            people.append(name)
    t = _PRICE.sub("<PRICE>", t)
    t = _DATE.sub("<DATE>", t)
    t = _NUM.sub("<NUM>", t)

    def bigram(m: re.Match) -> str:
        if _FIRST.fullmatch(m.group(1)):
            surface = f"{m.group(1)} {m.group(2)}"
            if surface not in other:
                other.append(surface)
        return "<PERSON>"

    t = _BIGRAM.sub(bigram, t)
    t = re.sub(r"<PERSON>(?:\s+<PERSON>)+", "<PERSON>", t)
    return Masked(text=t, people=people, other_names=other)


_TOKEN = re.compile(r"<[a-z]+>|\w+")


def tokens(text: str) -> list[str]:
    """Lower-cased word tokens; placeholders (``<person>``) stay single tokens."""
    return _TOKEN.findall(text.lower())
