"""Domain enrichment: registry, DNS and certificate facts for one domain, as contract Indicators.

For each domain the `Enricher` asks four keyless sources (RDAP, DoH, crt.sh, hole.cert.pl). Each
answer is cached with its fetch time and a TTL. A fresh cache entry is used as it is. Otherwise
the source is asked, unless the run is offline or the source has failed three times in a row.
If the source cannot answer, a stale cache entry is used and labelled as such. With no entry,
the indicators the source would have produced are emitted with value `unknown`. `enrich()`
never raises for network trouble.

Indicators are `layer=infrastructure`, `observed=live` (answers from a live lookup, or a cache
entry of one), and `provenance.note` names the source, the origin (live / cache / stale-cache),
the fetch time and the URL. Kinds and levels:

| kind | level | why this level |
|---|---|---|
| registrar | tenant | the registrar account a domain is bought through |
| registrar_iana_id | tenant | same entity as `registrar`, a stable id |
| created_date | landing | the registration of this exact landing name |
| domain_age_days_at_capture | landing | age of that name on the capture day |
| a_record_ip | operator | the server hosting the site; shared hosts are weak evidence, see the linking rules |
| nameserver | operator | a delegation set is run by one DNS operator, one per NS record |
| ns_provider | platform | registrable domain of the NS hosts, e.g. a DNS provider used by many operators |
| mx | operator | the mail host of the operator |
| cert_issuer | platform | the certificate authority (O= in the issuer name); shared by very many sites |
| cert_first_seen | landing | earliest certificate date for the name, which tracks go-live |
| cert_san_sibling | operator | other registrable names on the same certificate; usually one operator's sites |
| cert_pl_listed | landing | the CERT Polska list names this exact domain (`true`/`false`) |
| cert_pl_listed_date | landing | first insert date of the active listing |

`cert_san_sibling` is weak when a certificate covers many unrelated names (shared hosting or CDN
certificates). Linking should weight it by the number of names, which this module does not do.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ftf.contract import (
    Capture,
    Dataset,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
    Stage,
)
from ftf.enrich.cache import Cache, utc_iso
from ftf.enrich.http import PoliteClient, Unavailable
from ftf.enrich.normalise import registrable_domain
from ftf.enrich.sources import (
    CERT_PL_URL,
    OK,
    Fetched,
    fetch_bootstrap,
    fetch_cert_pl_index,
    fetch_crt,
    fetch_dns,
    fetch_rdap,
)

EXTRACTOR = "enrich-domain@0.1"
UNAVAILABLE = "unavailable"

SRC_BOOTSTRAP = "iana_rdap_bootstrap"
SRC_RDAP = "rdap"
SRC_DNS = "dns_doh"
SRC_CRT = "crt_sh"
SRC_CERT_PL = "cert_pl_hole"
SOURCES = (SRC_RDAP, SRC_DNS, SRC_CRT, SRC_CERT_PL)

# How long an answer stays fresh. Registration data changes rarely, DNS and the CERT list daily.
TTL = {
    SRC_BOOTSTRAP: timedelta(days=30),
    SRC_RDAP: timedelta(days=30),
    SRC_DNS: timedelta(days=1),
    SRC_CRT: timedelta(days=7),
    SRC_CERT_PL: timedelta(days=1),
}
BREAKER_THRESHOLD = 3  # consecutive failures before a source is skipped for the rest of the run

LEVELS = {
    "registrar": Level.tenant,
    "registrar_iana_id": Level.tenant,
    "created_date": Level.landing,
    "domain_age_days_at_capture": Level.landing,
    "a_record_ip": Level.operator,
    "nameserver": Level.operator,
    "ns_provider": Level.platform,
    "mx": Level.operator,
    "cert_issuer": Level.platform,
    "cert_first_seen": Level.landing,
    "cert_san_sibling": Level.operator,
    "cert_pl_listed": Level.landing,
    "cert_pl_listed_date": Level.landing,
}
# Kinds each source produces. If the source is unavailable with no cache, these get `unknown`.
UNKNOWN_KINDS = {
    SRC_RDAP: ("registrar", "registrar_iana_id", "created_date", "domain_age_days_at_capture"),
    SRC_DNS: ("a_record_ip", "nameserver", "ns_provider", "mx"),
    SRC_CRT: ("cert_issuer", "cert_first_seen", "cert_san_sibling"),
    SRC_CERT_PL: ("cert_pl_listed",),
}


@dataclass(frozen=True)
class Lookup:
    source: str
    domain: str
    status: str  # ok | not_found | unavailable
    origin: str  # live | cache | stale-cache | none
    data: dict[str, Any]
    fetched_at: str | None  # UTC time of the answer: the cache timestamp
    url: str | None
    reason: str | None = None


@dataclass(frozen=True)
class DomainResult:
    domain: str
    lookups: dict[str, Lookup]
    incident: Incident


class Enricher:
    """Cached, offline-capable enrichment of registrable domains."""

    def __init__(
        self,
        cache: Cache,
        *,
        client: PoliteClient | None = None,
        offline: bool = False,
        clock: Callable[[], datetime] | None = None,
        tld_cache: Path | None = None,
    ) -> None:
        self.cache = cache
        self.client = client or PoliteClient()
        self.offline = offline
        self.tld_cache = tld_cache
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures: Counter[str] = Counter()
        self._cert_pl_index: dict[str, dict[str, Any]] | None = None

    def registrable(self, value: str) -> str | None:
        return registrable_domain(value, tld_cache=self.tld_cache)

    def enrich(self, domain: str, *, captured_on: date | None = None) -> DomainResult:
        """Enrich one domain. Raises ValueError only if the input is not a registrable domain.

        `captured_on` is the capture date used for `domain_age_days_at_capture`. When it is not
        given, the lookup date is used and the provenance says so.
        """
        name = self.registrable(domain)
        if name is None:
            raise ValueError(f"not a registrable domain: {domain!r}")
        now = self._clock()
        lookups = {
            SRC_RDAP: self._lookup(SRC_RDAP, name, lambda: self._fetch_rdap(name), now),
            SRC_DNS: self._lookup(SRC_DNS, name, lambda: fetch_dns(self.client, name), now),
            SRC_CRT: self._lookup(SRC_CRT, name, lambda: fetch_crt(self.client, name, self.registrable), now),
            SRC_CERT_PL: self._lookup(SRC_CERT_PL, name, lambda: self._fetch_cert_pl(name), now),
        }
        used_date = captured_on or now.date()
        incident = _incident(
            name,
            lookups,
            captured_on=used_date,
            captured_given=captured_on is not None,
            registrable=self.registrable,
        )
        try:
            self.cache.save()
        except OSError as exc:
            self.cache.warnings.append(f"cache not saved: {exc}")
        return DomainResult(domain=name, lookups=lookups, incident=incident)

    # -- lookups -----------------------------------------------------------------------------

    def _lookup(self, source: str, domain: str, fetch: Callable[[], Fetched], now: datetime) -> Lookup:
        entry = self.cache.get(source, domain)
        if entry is not None and entry.fresh(now):
            return Lookup(source, domain, entry.status, "cache", entry.data, utc_iso(entry.fetched_at), entry.url)
        if self.offline:
            return self._fallback(source, domain, entry, "offline run")
        if self._failures[source] >= BREAKER_THRESHOLD:
            return self._fallback(source, domain, entry, "source skipped after repeated failures")
        try:
            fetched = fetch()
        except (Unavailable, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            self._failures[source] += 1
            return self._fallback(source, domain, entry, str(exc) or exc.__class__.__name__)
        self._failures[source] = 0
        self.cache.put(source, domain, status=fetched.status, data=fetched.data, fetched_at=now, ttl=TTL[source], url=fetched.url)
        return Lookup(source, domain, fetched.status, "live", fetched.data, utc_iso(now), fetched.url)

    @staticmethod
    def _fallback(source: str, domain: str, entry: Any, reason: str) -> Lookup:
        if entry is not None:
            return Lookup(source, domain, entry.status, "stale-cache", entry.data, utc_iso(entry.fetched_at), entry.url, reason)
        return Lookup(source, domain, UNAVAILABLE, "none", {}, None, None, reason)

    def _bootstrap_services(self, now: datetime) -> dict[str, list[str]]:
        boot = self._lookup(SRC_BOOTSTRAP, "*", lambda: fetch_bootstrap(self.client), now)
        if boot.status != OK:
            raise Unavailable(f"RDAP bootstrap {boot.origin}: {boot.reason or boot.status}")
        return boot.data["services"]

    def _fetch_rdap(self, domain: str) -> Fetched:
        services = self._bootstrap_services(self._clock())
        return fetch_rdap(self.client, domain, services)

    def _fetch_cert_pl(self, domain: str) -> Fetched:
        if self._cert_pl_index is None:
            self._cert_pl_index = fetch_cert_pl_index(self.client)  # one bulk download per run
        rec = self._cert_pl_index.get(domain, {"listed": False, "listed_date": None, "rows": 0})
        return Fetched(OK, {"listed": rec["listed"], "listed_date": rec["listed_date"], "rows": rec["rows"]}, CERT_PL_URL)


# -- contract output --------------------------------------------------------------------------


class _Indicators:
    def __init__(self, domain: str, capture_id: str) -> None:
        self.domain = domain
        self.capture_id = capture_id
        self.items: list[Indicator] = []
        self._count: Counter[str] = Counter()

    def add(self, kind: str, value: str, lookup: Lookup, locator: str | None, note: str | None = None) -> None:
        n = self._count[kind]
        self._count[kind] += 1
        parts = [f"source={lookup.source}", f"origin={lookup.origin}", f"fetched_at={lookup.fetched_at or 'none'}"]
        if lookup.url:
            parts.append(f"url={lookup.url}")
        if note:
            parts.append(note)
        self.items.append(
            Indicator(
                id=f"ind-{self.domain}-{kind}-{n}",
                capture_id=self.capture_id,
                layer=Layer.infrastructure,
                kind=kind,
                level=LEVELS[kind],
                value=value,
                observed=Observed.live,
                provenance=Provenance(locator=locator, note="; ".join(parts)),
                extractor=EXTRACTOR,
            )
        )

    def unknown(self, lookup: Lookup, kinds: tuple[str, ...]) -> None:
        for kind in kinds:
            self.add(kind, "unknown", lookup, locator=None, note=f"reason={lookup.reason or 'no answer'}")


def _indicators(
    domain: str,
    lookups: dict[str, Lookup],
    *,
    capture_id: str,
    captured_on: date,
    captured_given: bool,
    registrable: Callable[[str], str | None],
) -> list[Indicator]:
    out = _Indicators(domain, capture_id)

    rdap = lookups[SRC_RDAP]
    if rdap.status == UNAVAILABLE:
        out.unknown(rdap, UNKNOWN_KINDS[SRC_RDAP])
    elif rdap.status == OK:
        data = rdap.data
        if data.get("registrar"):
            out.add("registrar", data["registrar"], rdap, "entities[role=registrar].vcardArray.fn")
        if data.get("registrar_iana_id"):
            out.add("registrar_iana_id", data["registrar_iana_id"], rdap, "entities[role=registrar].publicIds")
        if data.get("created_date"):
            created = data["created_date"]
            out.add("created_date", created, rdap, "events[eventAction=registration].eventDate")
            age = (captured_on - date.fromisoformat(created)).days
            basis = "given" if captured_given else "assumed: lookup date"
            out.add("domain_age_days_at_capture", str(age), rdap, "derived: captured_on - created_date",
                    note=f"captured_on={captured_on.isoformat()} ({basis})")

    dns = lookups[SRC_DNS]
    if dns.status == UNAVAILABLE:
        out.unknown(dns, UNKNOWN_KINDS[SRC_DNS])
    elif dns.status == OK:
        for ip in dns.data.get("a", []):
            out.add("a_record_ip", ip, dns, "Answer[type=A].data")
        for ns in dns.data.get("ns", []):
            out.add("nameserver", ns, dns, "Answer[type=NS].data")
        providers = {registrable(ns) for ns in dns.data.get("ns", [])} - {None}
        for provider in sorted(providers):
            out.add("ns_provider", provider, dns, "derived: registrable domain of NS hosts")
        for mx in dns.data.get("mx", []):
            out.add("mx", mx, dns, "Answer[type=MX].data (host)")

    crt = lookups[SRC_CRT]
    if crt.status == UNAVAILABLE:
        out.unknown(crt, UNKNOWN_KINDS[SRC_CRT])
    elif crt.status == OK:
        for issuer in crt.data.get("issuers", []):
            out.add("cert_issuer", issuer, crt, "issuer_name O=")
        if crt.data.get("first_seen"):
            out.add("cert_first_seen", crt.data["first_seen"], crt, "min(not_before) over certificates")
        for sibling in crt.data.get("san_siblings", []):
            out.add("cert_san_sibling", sibling, crt, "name_value: other registrable domain")

    certpl = lookups[SRC_CERT_PL]
    if certpl.status == UNAVAILABLE:
        out.unknown(certpl, UNKNOWN_KINDS[SRC_CERT_PL])
    elif certpl.status == OK:
        listed = bool(certpl.data.get("listed"))
        out.add("cert_pl_listed", "true" if listed else "false", certpl,
                "DomainAddress == domain with DeleteDate null")
        if listed and certpl.data.get("listed_date"):
            out.add("cert_pl_listed_date", certpl.data["listed_date"], certpl, "InsertDate (earliest active row)")
    return out.items


def _incident(
    domain: str,
    lookups: dict[str, Lookup],
    *,
    captured_on: date,
    captured_given: bool,
    registrable: Callable[[str], str | None],
) -> Incident:
    """One enrichment incident per domain, with a single capture that stands for the domain's registry state."""
    capture_id = f"cap-{domain}"
    indicators = _indicators(
        domain,
        lookups,
        capture_id=capture_id,
        captured_on=captured_on,
        captured_given=captured_given,
        registrable=registrable,
    )
    return Incident(
        id=f"inc-{domain}",
        source="domain_enrichment",
        source_ref=domain,
        captures=[Capture(id=capture_id, stage=Stage.unknown, observed=Observed.live)],
        indicators=indicators,
    )


def build_dataset(results: list[DomainResult], *, name: str = "domain-enrichment", description: str | None = None) -> Dataset:
    """Contract Dataset holding one enrichment incident per domain."""
    return Dataset(name=name, description=description, incidents=[r.incident for r in results])
