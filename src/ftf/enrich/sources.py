"""Keyless lookup sources: request, parse, and say what the answer means.

Each `fetch_*` returns a `Fetched` (status `ok` or `not_found`) or raises `Unavailable`.
A 404 from RDAP or NXDOMAIN from DNS is an answer (`not_found`), not an outage.

Services (all free, no key, no account):
- IANA RDAP bootstrap (data.iana.org): which RDAP server runs each TLD.
- RDAP, the registry's own server: registrar, IANA registrar id, registration date.
- DNS over HTTPS (dns.google JSON API): A, NS, MX. The workbench asks a public resolver, so it
  never queries the scanned domain's own nameservers.
- crt.sh (Certificate Transparency search, JSON): issuers, earliest certificate date, names on
  the same certificates.
- CERT Polska warning list (hole.cert.pl, v2 JSON): one bulk download, matched locally, so the
  lookup never sends the domain anywhere.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ftf.enrich.http import Politeness, PoliteClient, Response, Unavailable

IANA_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DOH_URL = "https://dns.google/resolve"
CRT_URL = "https://crt.sh/"
CERT_PL_URL = "https://hole.cert.pl/domains/v2/domains.json"

OK = "ok"
NOT_FOUND = "not_found"

# Pacing per service. Intervals are per host. crt.sh is slow and returns 429/502 under load,
# so it gets the widest gap and the longest timeout. The hole.cert.pl list is about 22 MB.
POLICY = {
    "iana": Politeness(min_interval=1.0, timeout=30, retries=2, backoff=5),
    "rdap": Politeness(min_interval=1.0, timeout=30, retries=2, backoff=5),
    "dns": Politeness(min_interval=1.0, timeout=15, retries=2, backoff=3),
    "crt": Politeness(min_interval=5.0, timeout=150, retries=3, backoff=15),
    "cert_pl": Politeness(min_interval=0.0, timeout=300, retries=2, backoff=30),
}
CERT_PL_MAX_BYTES = 64 * 1024 * 1024

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOH_TYPES = {"A": 1, "NS": 2, "MX": 15}


@dataclass(frozen=True)
class Fetched:
    status: str  # OK or NOT_FOUND
    data: dict[str, Any]
    url: str | None


def _json(resp: Response, host: str) -> Any:
    try:
        return json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Unavailable(f"{host}: answer is not JSON") from exc


def _iso_date(value: Any) -> str | None:
    """Return the YYYY-MM-DD part of a timestamp when it is a valid calendar date."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    text = value[:10]
    if not _DATE.match(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


# --- IANA bootstrap and RDAP --------------------------------------------------------------


def rdap_services(body: Any) -> dict[str, list[str]]:
    """Map each TLD to its https RDAP base URLs, from the IANA bootstrap file."""
    services: dict[str, list[str]] = {}
    for entry in body["services"]:
        tlds, urls = entry[0], entry[1]
        https_urls = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
        if not https_urls:
            continue
        for tld in tlds:
            services[str(tld).lower()] = https_urls
    return services


def rdap_base(domain: str, services: dict[str, list[str]]) -> str | None:
    urls = services.get(domain.rsplit(".", 1)[-1].lower())
    if not urls:
        return None
    base = urls[0]
    return base if base.endswith("/") else base + "/"


def _vcard_value(entity: dict[str, Any], field_name: str) -> str | None:
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2:
        return None
    for prop in card[1]:
        if isinstance(prop, list) and len(prop) >= 4 and prop[0] == field_name:
            value = prop[3]
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_rdap(body: dict[str, Any]) -> dict[str, Any]:
    """Registrar name, IANA registrar id and registration date from an RDAP domain object."""
    registrar = None
    iana_id = None
    for entity in body.get("entities") or []:
        if not isinstance(entity, dict) or "registrar" not in (entity.get("roles") or []):
            continue
        registrar = _vcard_value(entity, "fn") or registrar
        for pid in entity.get("publicIds") or []:
            if isinstance(pid, dict) and pid.get("type") == "IANA Registrar ID":
                iana_id = str(pid.get("identifier")) if pid.get("identifier") is not None else iana_id
    created = None
    for event in body.get("events") or []:
        if isinstance(event, dict) and event.get("eventAction") == "registration":
            created = _iso_date(event.get("eventDate")) or created
    return {"registrar": registrar, "registrar_iana_id": iana_id, "created_date": created}


def fetch_bootstrap(client: PoliteClient) -> Fetched:
    resp = client.get(IANA_BOOTSTRAP_URL, POLICY["iana"])
    if resp.status != 200:
        raise Unavailable(f"IANA bootstrap HTTP {resp.status}")
    return Fetched(OK, {"services": rdap_services(_json(resp, "data.iana.org"))}, IANA_BOOTSTRAP_URL)


def fetch_rdap(client: PoliteClient, domain: str, services: dict[str, list[str]]) -> Fetched:
    base = rdap_base(domain, services)
    if base is None:
        return Fetched(NOT_FOUND, {"reason": "no RDAP service for this TLD"}, None)
    url = f"{base}domain/{quote(domain)}"
    resp = client.get(url, POLICY["rdap"], accept="application/rdap+json")
    if resp.status == 404:
        return Fetched(NOT_FOUND, {}, url)
    if resp.status != 200:
        raise Unavailable(f"RDAP HTTP {resp.status}")
    body = _json(resp, urlsplit(url).netloc)
    if not isinstance(body, dict):
        raise Unavailable("RDAP: unexpected answer")
    return Fetched(OK, parse_rdap(body), url)


# --- DNS over HTTPS -----------------------------------------------------------------------


def _doh_answers(body: dict[str, Any], rtype: str) -> list[str]:
    """Answers of one record type, normalised. Null MX (`0 .`) yields nothing."""
    code = _DOH_TYPES[rtype]
    out: list[str] = []
    for rec in body.get("Answer") or []:
        if not isinstance(rec, dict) or rec.get("type") != code:
            continue
        data = str(rec.get("data", "")).strip()
        if rtype == "A":
            try:
                out.append(str(ipaddress.IPv4Address(data)))
            except ValueError:
                continue
        elif rtype == "NS":
            out.append(data.rstrip(".").lower())
        else:  # MX: "<preference> <host>."
            parts = data.split()
            if len(parts) == 2:
                out.append(parts[1].rstrip(".").lower())
    return sorted({value for value in out if value})


def fetch_dns(client: PoliteClient, domain: str) -> Fetched:
    """A, NS and MX via DoH. Any SERVFAIL-like error makes the whole lookup unavailable."""
    answers: dict[str, list[str]] = {}
    rcodes: dict[str, int] = {}
    for rtype in ("A", "NS", "MX"):
        url = f"{DOH_URL}?{urlencode({'name': domain, 'type': rtype})}"
        resp = client.get(url, POLICY["dns"], accept="application/dns-json")
        if resp.status != 200:
            raise Unavailable(f"DoH {rtype} HTTP {resp.status}")
        body = _json(resp, "dns.google")
        if not isinstance(body, dict):
            raise Unavailable("DoH: unexpected answer")
        rcode = body.get("Status")
        if rcode not in (0, 3):  # 0 NOERROR, 3 NXDOMAIN; anything else is not an answer
            raise Unavailable(f"DoH {rtype} rcode {rcode}")
        rcodes[rtype] = rcode
        answers[rtype] = _doh_answers(body, rtype)
    url = f"{DOH_URL}?name={domain}&type=A,NS,MX"
    if all(code == 3 for code in rcodes.values()):
        return Fetched(NOT_FOUND, {"a": [], "ns": [], "mx": []}, url)
    return Fetched(OK, {"a": answers["A"], "ns": answers["NS"], "mx": answers["MX"]}, url)


# --- Certificate Transparency (crt.sh) ----------------------------------------------------

_ORG = re.compile(r"(?:^|,\s*)O=([^,]+)")


def issuer_org(issuer_name: Any) -> str | None:
    """The O= part of a crt.sh issuer name, or the whole name if it has none."""
    if not isinstance(issuer_name, str) or not issuer_name.strip():
        return None
    match = _ORG.search(issuer_name)
    return match.group(1).strip() if match else issuer_name.strip()


def parse_crt(rows: list[dict[str, Any]], domain: str, registrable: Any) -> dict[str, Any]:
    """Summarise crt.sh rows for one domain.

    `first_seen` is the earliest `not_before` (the JSON endpoint has no log entry time).
    `san_siblings` are the other registrable domains named on the same certificates; names
    under the queried domain itself are not siblings. Free-text or e-mail entries that do not
    parse as hosts are ignored.
    """
    issuers: set[str] = set()
    siblings: set[str] = set()
    first_seen: str | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        issuer = issuer_org(row.get("issuer_name"))
        if issuer:
            issuers.add(issuer)
        seen = _iso_date(row.get("not_before"))
        if seen and (first_seen is None or seen < first_seen):
            first_seen = seen
        for name in str(row.get("name_value") or "").split("\n"):
            other = registrable(name.strip())
            if other and other != domain:
                siblings.add(other)
    return {
        "certificates": len(rows),
        "issuers": sorted(issuers),
        "first_seen": first_seen,
        "san_siblings": sorted(siblings),
    }


def fetch_crt(client: PoliteClient, domain: str, registrable: Any) -> Fetched:
    url = f"{CRT_URL}?{urlencode({'q': domain, 'output': 'json'})}"
    resp = client.get(url, POLICY["crt"])
    if resp.status != 200:
        raise Unavailable(f"crt.sh HTTP {resp.status}")
    rows = _json(resp, "crt.sh")
    if not isinstance(rows, list):
        raise Unavailable("crt.sh: unexpected answer")
    if not rows:
        return Fetched(NOT_FOUND, {"certificates": 0, "issuers": [], "first_seen": None, "san_siblings": []}, url)
    return Fetched(OK, parse_crt(rows, domain, registrable), url)


# --- CERT Polska warning list -------------------------------------------------------------


def parse_cert_pl(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the list by domain: listed if any row has no DeleteDate, listed_date is the earliest such InsertDate."""
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("DomainAddress") or "").strip().lower().rstrip(".")
        if not name:
            continue
        rec = index.setdefault(name, {"listed": False, "listed_date": None, "rows": 0})
        rec["rows"] += 1
        if row.get("DeleteDate") is None:
            rec["listed"] = True
            inserted = _iso_date(row.get("InsertDate"))
            if inserted and (rec["listed_date"] is None or inserted < rec["listed_date"]):
                rec["listed_date"] = inserted
    return index


def fetch_cert_pl_index(client: PoliteClient) -> dict[str, dict[str, Any]]:
    resp = client.get(CERT_PL_URL, POLICY["cert_pl"], max_bytes=CERT_PL_MAX_BYTES)
    if resp.status != 200:
        raise Unavailable(f"hole.cert.pl HTTP {resp.status}")
    rows = _json(resp, "hole.cert.pl")
    if not isinstance(rows, list):
        raise Unavailable("hole.cert.pl: unexpected answer")
    return parse_cert_pl(rows)
