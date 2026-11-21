"""Keyless domain enrichment (HACK-19): RDAP, DNS over HTTPS, crt.sh and the CERT Polska list.

Public surface: `Enricher` (cached, offline-capable lookups returning contract Indicators),
`Cache` (one JSON file, also the offline export), `organiser_domains` (the domains the
organiser's data names) and `build_dataset`. The `ftf enrich-domain` command is in `commands`.
"""

from ftf.enrich.cache import Cache
from ftf.enrich.domain import DomainResult, Enricher, Lookup, build_dataset
from ftf.enrich.normalise import registrable_domain
from ftf.enrich.scope import organiser_domains

__all__ = [
    "Cache",
    "DomainResult",
    "Enricher",
    "Lookup",
    "build_dataset",
    "organiser_domains",
    "registrable_domain",
]
