"""Gazetteer of public figures whose likeness or name is frequently abused in scam ads.

Shared by the text family (name masking, ``person_mention``) and the faces family.

Sources: public knowledge, chiefly figures repeatedly named in public CERT Polska / NASK warnings
and press reports about fake investment and health ads that use a celebrity's likeness.
It is a plain list on purpose: extend it
when a new case names someone. Names are written in the nominative; matching is done on the
surname stem so Polish inflection ("Lewandowskiego", "Kulczykowi") still matches.
"""

from __future__ import annotations

PEOPLE: list[str] = [
    # Business
    "Rafał Brzoska",
    "Omena Mensah",
    "Dominika Kulczyk",
    "Jan Kulczyk",
    "Michał Sołowow",
    "Zygmunt Solorz",
    "Leszek Czarnecki",
    "Elon Musk",
    # Journalists and media
    "Krzysztof Stanowski",
    "Tomasz Sekielski",
    "Tomasz Lis",
    "Monika Olejnik",
    "Piotr Kraśko",
    "Bogdan Rymanowski",
    "Justyna Pochanke",
    "Kuba Wojewódzki",
    "Marcin Prokop",
    "Dorota Wellman",
    "Tomasz Kammel",
    "Krzysztof Ibisz",
    "Martyna Wojciechowska",
    "Magda Gessler",
    "Małgorzata Rozenek-Majdan",
    "Szymon Majewski",
    "Natalia Pacholczyk",
    # Politics and public institutions
    "Donald Tusk",
    "Andrzej Duda",
    "Karol Nawrocki",
    "Mateusz Morawiecki",
    "Rafał Trzaskowski",
    "Szymon Hołownia",
    "Andrzej Domański",
    "Adam Glapiński",
    "Jarosław Kaczyński",
    "Sławomir Mentzen",
    # Sport
    "Robert Lewandowski",
    "Anna Lewandowska",
    "Iga Świątek",
    "Cristiano Ronaldo",
    "Mariusz Pudzianowski",
    "Adam Małysz",
    "Kamil Stoch",
    "Wojciech Szczęsny",
    "Agnieszka Radwańska",
    # Health and lifestyle
    "Ewa Chodakowska",
    "Jerzy Owsiak",
    "Cezary Pazura",
]
