"""Source-neutral ad record and its conversion to a contract `Incident`.

Every source parser (Meta Ad Library, Google Ads Transparency Center, CERT Polska list) turns
its raw response into `AdRecord`s; `to_incident` maps one record to one contract Incident with
a single `ad` capture (or `unknown` for a domain-only listing) and its indicators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ftf import __version__
from ftf.contract import (
    Artefact,
    ArtefactType,
    Capture,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
    Stage,
)

EXTRACTOR = f"ftf-scanner@{__version__}"
SOURCES = ("meta", "google_atc", "google_bq", "certpl")

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.\-]+")
_MAX_TEXT = 4000


@dataclass
class AdRecord:
    """One ad (or one listed domain) as found in a public library."""

    source: str  # one of SOURCES
    ad_id: str  # the library's own id (Meta ad_archive_id, Google CR… id, CERT register id)
    keyword: Optional[str] = None  # the search term that found it (None for a full-list scan)
    page_id: Optional[str] = None  # Meta page id
    page_name: Optional[str] = None
    advertiser_id: Optional[str] = None  # Google AR… id
    advertiser_name: Optional[str] = None
    body: Optional[str] = None
    title: Optional[str] = None
    link_description: Optional[str] = None
    display_domain: Optional[str] = None  # caption shown in the ad (Meta) or ad domain (Google)
    landing_url: Optional[str] = None  # declared to the platform; never fetched by the scanner
    landing_domain: Optional[str] = None  # set when only a domain is known (Google, CERT)
    creative_urls: list[str] = field(default_factory=list)
    collation_id: Optional[str] = None  # Meta "N ads use this creative and text" group
    display_format: Optional[str] = None
    platforms: list[str] = field(default_factory=list)
    start_date: Optional[str] = None  # YYYY-MM-DD, first shown / listed
    end_date: Optional[str] = None  # YYYY-MM-DD, last shown / delisted
    status: Optional[str] = None  # e.g. Meta gated_type TAKEN_DOWN, CERT "listed"
    register_id: Optional[str] = None  # CERT Polska register position (ad_id is the registrable domain)
    declared_link_url: Optional[str] = None  # link as given when it stays on Meta (fb.me, facebook.com)

    @property
    def key(self) -> str:
        """Deduplication key across runs: source plus the library's own id."""
        # Both Google sources name the same creative ids: one key, so a creative is queued once.
        group = "google_atc" if self.source == "google_bq" else self.source
        return f"{group}:{self.ad_id}"

    @property
    def incident_id(self) -> str:
        """Cross-project id (HACK-41): `adlib:<ad archive id>` for ads (Meta archive id, Google
        creative id `CR…`); CERT Polska entries are bare domains, `certpl:<registrable domain>`.
        Never depends on a timestamp."""
        prefix = "certpl" if self.source == "certpl" else "adlib"
        return f"{prefix}:{self.ad_id}"

    @property
    def capture_ref(self) -> str:
        """Contract capture id: `<incident id>:ad` (or `:listing` for a CERT domain)."""
        return f"{self.incident_id}:{'listing' if self.source == 'certpl' else 'ad'}"

    def folder(self, stamp: str) -> str:
        """Capture folder name: `<run stamp>-<source>-<ad id>` (only the folder carries a time)."""
        return f"{stamp}-{self.source.replace('_', '-')}-{_SAFE_ID.sub('_', self.ad_id)}"

    def effective_landing_domain(self) -> Optional[str]:
        return host_of(self.landing_url) if self.landing_url else normalise_domain(self.landing_domain)


def unix_to_date(value) -> Optional[str]:
    """Unix seconds (int or numeric string) to YYYY-MM-DD in UTC; None if absent or invalid."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalise_domain(value: Optional[str]) -> Optional[str]:
    """Lower-case host name without a trailing dot, a port or a leading `www.`; IDN to punycode."""
    if not value:
        return None
    host = value.strip().lower().rstrip(".")
    if "://" in host:
        return host_of(host)
    host = host.split("/", 1)[0].split(":", 1)[0]
    if not host or " " in host or "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    return host or None


_PLATFORM_HOSTS = ("facebook.com", "fb.me", "fb.com", "m.me", "instagram.com", "messenger.com", "wa.me", "whatsapp.com", "threads.net")


def is_platform_link(url: str) -> bool:
    """True for links that stay on Meta (fb.me shims, page or Messenger links): not a landing page."""
    host = host_of(url) or ""
    return any(host == h or host.endswith("." + h) for h in _PLATFORM_HOSTS)


def unwrap_redirect(url: str) -> str:
    """Strip Meta's outbound redirector (l.facebook.com/l.php?u=…) to the declared target."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host in {"l.facebook.com", "lm.facebook.com", "l.instagram.com"} and parts.path.rstrip("/") == "/l.php":
        target = parse_qs(parts.query).get("u")
        if target and target[0].lower().startswith(("http://", "https://")):
            return target[0]
    return url


def host_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return normalise_domain(host) if host else None


def clean_text(value) -> Optional[str]:
    """Collapse whitespace, drop empty strings and cap length; non-strings become None."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    return text[:_MAX_TEXT]


def to_incident(rec: AdRecord, captured_on: date, creative_artefacts: Optional[list[Artefact]] = None) -> Incident:
    """Map one record to a provisional contract Incident (one capture, static metadata indicators).

    Ids follow the cross-project scheme (see `AdRecord.incident_id`); Ingestion's adapter builds
    the canonical Incident from the capture folder.
    """
    cap_id = rec.capture_ref
    inc_id = rec.incident_id
    is_domain_listing = rec.source == "certpl"
    lib_art = Artefact(
        id=f"{cap_id}:library",
        type=ArtefactType.url if is_domain_listing else ArtefactType.ad_library,
    )
    artefacts = [lib_art, *(creative_artefacts or [])]
    capture = Capture(
        id=cap_id,
        stage=Stage.unknown if is_domain_listing else Stage.ad,
        observed=Observed.static,
        captured_on=captured_on.isoformat(),
        artefacts=artefacts,
    )

    indicators: list[Indicator] = []
    counts: dict[str, int] = {}

    def add(layer: Layer, level: Level, kind: str, value: Optional[str], locator: str) -> None:
        if value is None or value == "":
            return
        n = counts.get(kind, 0)
        counts[kind] = n + 1
        indicators.append(
            Indicator(
                id=f"{cap_id}:{kind}:{n}",
                capture_id=cap_id,
                layer=layer,
                kind=kind,
                level=level,
                value=str(value),
                observed=Observed.static,
                provenance=Provenance(artefact_id=lib_art.id, locator=locator),
                extractor=EXTRACTOR,
            )
        )

    if is_domain_listing:
        add(Layer.landing, Level.landing, "cert_register_id", rec.register_id, "RegisterPositionId")
    else:
        add(Layer.creative, Level.capture, f"{rec.source}_ad_id", rec.ad_id, "ad_id")
    add(Layer.distribution_account, Level.operator, "advertiser_page_id", rec.page_id, "page_id")
    add(Layer.distribution_account, Level.operator, "advertiser_page_name", rec.page_name, "page_name")
    add(Layer.distribution_account, Level.operator, "advertiser_id", rec.advertiser_id, "advertiser_id")
    add(Layer.distribution_account, Level.operator, "advertiser_name", rec.advertiser_name, "advertiser_name")
    add(Layer.creative, Level.campaign, "ad_text", rec.body, "body")
    add(Layer.creative, Level.campaign, "ad_title", rec.title, "title")
    add(Layer.creative, Level.campaign, "ad_link_description", rec.link_description, "link_description")
    add(Layer.creative, Level.campaign, "ad_collation_id", rec.collation_id, "collation_id")
    add(Layer.creative, Level.campaign, "ad_display_domain", normalise_domain(rec.display_domain), "caption")
    add(Layer.landing, Level.landing, "landing_url", rec.landing_url, "link_url")
    add(Layer.landing, Level.landing, "declared_link_url", rec.declared_link_url, "link_url")
    domain_from = "link_url" if rec.landing_url else ("DomainAddress" if is_domain_listing else "advertiser_domain")
    add(Layer.landing, Level.landing, "landing_domain", rec.effective_landing_domain(), domain_from)
    for url in rec.creative_urls:
        add(Layer.creative, Level.capture, "creative_url", url, "creative")
    add(Layer.creative, Level.capture, "ad_display_format", rec.display_format, "display_format")
    for platform in rec.platforms:
        add(Layer.distribution_account, Level.ecosystem, "ad_platform", platform, "publisher_platform")
    add(Layer.creative, Level.capture, "ad_start_date", rec.start_date, "start_date")
    add(Layer.creative, Level.capture, "ad_end_date", rec.end_date, "end_date")
    add(Layer.creative, Level.capture, "ad_library_status", rec.status, "status")
    # The search term describes our scanner, not the scam: collector layer, never linked.
    add(Layer.collector, Level.capture, "scan_keyword", rec.keyword, "query")

    return Incident(
        id=inc_id,
        source=f"scanner_{rec.source}",
        source_ref=rec.ad_id,
        captures=[capture],
        indicators=indicators,
    )
