"""Keyless domain enrichment: RDAP, DNS over HTTPS, crt.sh and the CERT Polska list.

Public surface: `Enricher` (cached, offline-capable lookups returning contract Indicators),
`Cache` (one JSON file, also the offline export) and `build_dataset`. The domains to look up come
from `ftf.indicators.domains.registered_domains`. The `ftf enrich-domain` command is in
`commands`.
"""

from ftf.enrich.cache import Cache
from ftf.enrich.domain import DomainResult, Enricher, Lookup, build_dataset
from ftf.enrich.normalise import registrable_domain

__all__ = [
    "Cache",
    "DomainResult",
    "Enricher",
    "Lookup",
    "build_dataset",
    "registrable_domain",
]
