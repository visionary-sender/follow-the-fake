"""Own host, registered-domain and redirect-chain indicators (HACK-48).

Which hosts the page itself lives on: the landing, every main-frame hop before it (tracker,
cloaker, pre-lander), the landing named by an ad-library record and a CERT Polska listing.
Page URLs come from `_pages.page_chain`, the same function kit.py uses for its own-versus-third-
party split. Hosts and registered domains go through `ftf.enrich.normalise` (offline Public
Suffix List snapshot; set `TLDEXTRACT_CACHE`, e.g. `/opt/tldextract-cache` on the VM). Values are
hosts or domains only, never URLs, so no query string, click id or token can reach a value.

| kind | layer | level | observed | meaning |
|---|---|---|---|---|
| `landing_host` | landing | landing | live / static | host of the landing page (last main-frame URL; saved-from, MHTML location, url file) |
| `landing_domain` | landing | landing | live / static | its registered domain; also a CERT Polska listing's domain |
| `chain_host` | tracker_instance / infrastructure | operator / campaign | live | host of a main-frame hop before the landing (another registered domain than the landing's) |
| `chain_domain` | tracker_instance / infrastructure | operator / campaign | live | its registered domain |
| `redirect_chain_shape` | landing | platform | live | hop roles and transitions of one HAR's main-frame chain, no hosts or ids |
| `ad_landing_domain` | landing | landing | static | registered domain of an ad-library record's landing URL (else its landing domain) |
| `ad_display_domain` | creative | campaign | static | registered domain shown in the ad (the record's display domain) |

A chain hop is `tracker_instance` / `operator` when its URL looks like a click tracker (a click-id
parameter such as `clickid` or `subid`, or a `/click` path segment), else `infrastructure` /
`campaign` (cloaker, pre-lander, redirector).

`redirect_chain_shape` is e.g. `start:shared>302:tracker>302:self>200:landing`: per hop its role
(`shared` = stop-listed platform redirector such as a social network's link shim, `tracker`, `hop`
= other foreign domain, `self` = the landing's own registered domain, `landing`) after the
transition that led there (`start`, a 3xx status, or `nav` for a new page). It is layer
`landing` / level `platform`: the shape describes the traffic setup (TDS), not one operator.
Only HAR chains of two or more hops get one. Its `provenance.note` lists the shared hops'
registered domains (`shared=a,b`), because the stop-list drops shared hop values from the dataset
while `domain_roles` must still be able to name them.

Stop-list: shared hosts (platform redirectors, SDK hosts, brand sites) are dropped by
`extract_all` like every other host kind, so a redirect through a link shim never becomes linking
evidence. `registered_domains(dataset)` answers "which domains do this dataset's pages live on",
for `ftf.enrich`; `domain_roles(dataset)` says in which role each was seen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ftf import __version__
from ftf.contract import ArtefactType, Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Provenance
from ftf.enrich.normalise import hostname, registrable_domain
from ftf.indicators import _pages, _stoplist
from ftf.indicators._pages import CHAIN, LANDING
from ftf.ingest.adlib import ad_artefact, load_ad

EXTRACTOR = f"domains@{__version__}"
FETCH_SOURCE = "live_fetch"
CERTPL_SOURCE = "certpl"

_KINDS: dict[str, tuple[Layer, Level]] = {
    "landing_host": (Layer.landing, Level.landing),
    "landing_domain": (Layer.landing, Level.landing),
    "chain_host": (Layer.infrastructure, Level.campaign),
    "chain_domain": (Layer.infrastructure, Level.campaign),
    "redirect_chain_shape": (Layer.landing, Level.platform),
    "ad_landing_domain": (Layer.landing, Level.landing),
    "ad_display_domain": (Layer.creative, Level.campaign),
}
_TRACKER = (Layer.tracker_instance, Level.operator)

_CLICK_PARAMS = frozenset({"clickid", "click_id", "upd_clickid", "subid", "sub_id", "bcid"})
_CLICK_PATH = re.compile(r"(?:^|/)(?:click|clk)(?:/|\.php|$)", re.I)

ROLES = ("landing", "redirect_hop", "ad_landing", "ad_display", "certpl", "shared")
_ROLE_OF_KIND = {
    "landing_domain": "landing",
    "chain_domain": "redirect_hop",
    "ad_landing_domain": "ad_landing",
    "ad_display_domain": "ad_display",
}
_ENRICH_ROLES = frozenset({"landing", "redirect_hop", "ad_landing", "certpl"})


def _ind(capture: Capture, kind: str, value: str, observed: Observed, art: str, locator: str,
         *, tracker: bool = False, note: str | None = None) -> Indicator:
    layer, level = _TRACKER if tracker else _KINDS[kind]
    return Indicator(
        id="dom0",
        capture_id=capture.id,
        layer=layer,
        kind=kind,
        level=level,
        value=value,
        observed=observed,
        provenance=Provenance(artefact_id=art, locator=locator, note=note),
        extractor=EXTRACTOR,
    )


def is_shared(host_or_domain: str) -> bool:
    """True if the HACK-12 stop-list names this host (a platform, CDN, SDK, brand or redirector)."""
    host = hostname(host_or_domain)
    if host is None:
        return False
    L = _stoplist.lists()
    return host in L.bare or _stoplist.host_stopped(host, L.all_hosts)


def looks_like_tracker(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    names = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    return bool(names & _CLICK_PARAMS) or bool(_CLICK_PATH.search(parts.path))


def extract(incident: Incident, capture: Capture, root: Path) -> Iterable[Indicator]:
    out: list[Indicator] = []
    if incident.source == CERTPL_SOURCE or capture.id.endswith(":listing") and incident.id.startswith("certpl:"):
        out.extend(_certpl(incident, capture))
    out.extend(_ad(capture, root))
    chain = _pages.page_chain(capture, root, fetched=incident.source == FETCH_SOURCE)
    out.extend(_hosts(capture, chain))
    for art in capture.artefacts:
        if art.type is ArtefactType.har:
            out.extend(_shapes(capture, root, art))
    return out


def _hosts(capture: Capture, chain: list[_pages.PageUrl]) -> list[Indicator]:
    out: list[Indicator] = []
    landing_hosts = {hostname(p.url) for p in chain if p.role == LANDING} - {None}
    landing_domains = {registrable_domain(h) for h in landing_hosts} - {None}
    for p in chain:
        host = hostname(p.url)
        if host is None:
            continue
        domain = registrable_domain(host)
        loc = p.locator
        if p.role == LANDING:
            out.append(_ind(capture, "landing_host", host, p.observed, p.artefact_id, loc))
            if domain:
                out.append(_ind(capture, "landing_domain", domain, p.observed, p.artefact_id, loc))
        elif p.role == CHAIN and host not in landing_hosts and domain not in landing_domains:
            tracker = looks_like_tracker(p.url) and not is_shared(host)
            out.append(_ind(capture, "chain_host", host, p.observed, p.artefact_id, loc, tracker=tracker))
            if domain:
                out.append(_ind(capture, "chain_domain", domain, p.observed, p.artefact_id, loc, tracker=tracker))
    return out


def _shapes(capture: Capture, root: Path, art) -> list[Indicator]:
    hops = [h for page in _pages._har_file(root, art) for h in page]
    if len(hops) < 2:
        return []
    landing = registrable_domain(hops[-1].url)
    tokens: list[str] = []
    shared: list[str] = []
    for n, h in enumerate(hops):
        domain = registrable_domain(h.url)
        if n == len(hops) - 1:
            role = "landing"
        elif domain and is_shared(h.url):
            role = "shared"
            if domain not in shared:
                shared.append(domain)
        elif domain is not None and domain == landing:
            role = "self"
        elif looks_like_tracker(h.url):
            role = "tracker"
        else:
            role = "hop"
        tokens.append(f"{h.via}:{role}")
    note = "shared=" + ",".join(shared) if shared else None
    return [_ind(capture, "redirect_chain_shape", ">".join(tokens), Observed.live, art.id, hops[0].locator, note=note)]


def _ad(capture: Capture, root: Path) -> list[Indicator]:
    art = ad_artefact(capture)
    if art is None:
        return []
    rec = load_ad(capture, root)
    out: list[Indicator] = []
    for field in ("landing_url", "landing_domain"):
        v = rec.get(field)
        domain = registrable_domain(v) if isinstance(v, str) and v else None
        if domain:
            out.append(_ind(capture, "ad_landing_domain", domain, Observed.static, art.id, f"$.{field}"))
            break
    v = rec.get("display_domain")
    domain = registrable_domain(v) if isinstance(v, str) and v else None
    if domain:
        out.append(_ind(capture, "ad_display_domain", domain, Observed.static, art.id, "$.display_domain"))
    return out


def _certpl(incident: Incident, capture: Capture) -> list[Indicator]:
    listed = incident.source_ref or incident.id.split(":", 1)[-1]
    domain = registrable_domain(listed) if listed else None
    url_art = next((a for a in capture.artefacts if a.type is ArtefactType.url), None) or (capture.artefacts[0] if capture.artefacts else None)
    if domain is None or url_art is None:
        return []
    return [_ind(capture, "landing_domain", domain, Observed.static, url_art.id, "incident.source_ref")]


# --------------------------------------------------------------------------- dataset helpers


def domain_roles(dataset: Dataset) -> dict[str, set[str]]:
    """Registered domain -> roles from {landing, redirect_hop, ad_landing, ad_display, certpl, shared}.

    Built from the indicators this module emitted. `shared` comes from the stop-list: hop domains
    the stop-list dropped are read back from `redirect_chain_shape` notes, and any emitted domain
    the stop-list names (a dataset built without it) is marked too.
    """
    roles: dict[str, set[str]] = {}
    for inc in dataset.incidents:
        certpl = inc.source == CERTPL_SOURCE
        for ind in inc.indicators:
            if not ind.extractor.startswith("domains@") or ind.value is None:
                continue
            if ind.kind == "redirect_chain_shape":
                note = ind.provenance.note or ""
                if note.startswith("shared="):
                    for d in note[len("shared="):].split(","):
                        if d:
                            roles.setdefault(d, set()).add("shared")
                continue
            role = _ROLE_OF_KIND.get(ind.kind)
            if role is None:
                continue
            if certpl and role == "landing":
                role = "certpl"
            roles.setdefault(ind.value, set()).add(role)
            if is_shared(ind.value):
                roles[ind.value].add("shared")
    return roles


def registered_domains(dataset: Dataset, include_shared: bool = False) -> list[str]:
    """Sorted registered domains the dataset's pages live on (landing, redirect hop, ad landing,
    CERT listing), ready for `ftf.enrich`. Ad display domains are left out (an ad may show a brand
    it does not control). Shared domains are left out unless `include_shared`."""
    out = set()
    for domain, roles in domain_roles(dataset).items():
        if "shared" in roles:
            if include_shared:
                out.add(domain)
        elif roles & _ENRICH_ROLES:
            out.add(domain)
    return sorted(out)


def shared_category(domain: str) -> str:
    """Stop-list group a shared registered domain falls in (first match), or `other`."""
    for name, entries in _stoplist.groups().items():
        if domain in entries or (name != "bare_domain" and _stoplist.host_stopped(domain, entries)):
            return name
    return "other"


def shared_dropped(dataset: Dataset) -> dict[str, int]:
    """Count of shared registered domains `registered_domains` leaves out, by stop-list group.

    Counts and categories only: the domain names themselves are never returned, because the
    organiser's data names must not leave the outputs bucket."""
    kept = set(registered_domains(dataset, include_shared=False))
    dropped = [d for d in registered_domains(dataset, include_shared=True) if d not in kept]
    counts: dict[str, int] = {}
    for d in dropped:
        cat = shared_category(d)
        counts[cat] = counts.get(cat, 0) + 1
    return dict(sorted(counts.items()))


def kinds() -> dict[str, tuple[Layer, Level]]:
    """Kind -> (layer, level) for every kind this module emits (chain kinds may also be tracker_instance/operator)."""
    return dict(_KINDS)


__all__ = ["extract", "kinds", "registered_domains", "domain_roles", "is_shared", "shared_category", "shared_dropped", "EXTRACTOR"]
