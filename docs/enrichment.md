The README summarises the metadata sources. This page holds the services, their terms and limits, and the indicator kinds.

`ftf enrich-domain` looks up registrable domains in four keyless services and writes contract
`Indicator`s (layer `infrastructure`, observed `live`). Each answer carries its fetch time and
its origin (live, cache or stale-cache). Answers are cached in one JSON file, which is also the
offline export. With no network and no cache entry, the indicators come out as `unknown`.

```bash
ftf enrich-domain example.com --cache data/enrichment/cache.json --out enrichment.json
ftf enrich-domain --from-dataset dataset.json --offline --cache cache.json --summary summary.json
ftf enrich-domain --from-raw /data/raw --offline --cache cache.json --summary summary.json
```

`--from-dataset PATH` takes the registered domains the dataset's pages live on: the landing,
each redirect hop, the ad landing and a CERT Polska listing, from `registered_domains(dataset,
include_shared=False)`. Shared platforms, CDNs and SDK hosts (the stop-list) are left out, and the
summary counts them by stop-list group only. Ad display domains are left out too, since an ad may
show a brand it does not control. `--from-raw DIR` is a thin wrapper: it ingests the folder
(organiser data, saved pages, HARs, read as inert data; all extractors run, so it is slower) and
then does the same. An input that cannot be read is skipped with a warning, and the summary lists
it. The organiser's `scam_links*.txt` lists are not read by this path: the ingested dataset has no
incident for them, so their domains are not enriched. Registrable domains come from tldextract
with its bundled Public Suffix List snapshot, so nothing is downloaded to normalise a name.

Kinds: `registrar`, `registrar_iana_id`, `created_date`, `domain_age_days_at_capture`,
`a_record_ip`, `nameserver`, `ns_provider`, `mx`, `cert_issuer`, `cert_first_seen`,
`cert_san_sibling`, `cert_pl_listed` (`true`/`false`), `cert_pl_listed_date`. The levels and
their reasons are in the docstring of `src/ftf/enrich/domain.py`. `ip_asn` is not produced: the
only keyless ASN data is a bulk table that would have to ship with the container, which this
issue does not do.

### Sources, terms and limits

Only these services are contacted, and never the scanned domain's own web server or nameservers.
DNS goes through a public DoH resolver. Nothing is submitted to scanning services. The User-Agent
is generic (`ftf-domain-enrichment/0.1 (+keyless registry lookups; research)`).

| Service | Used for | Terms (as recorded) | Our limits |
|---|---|---|---|
| IANA RDAP bootstrap, `data.iana.org/rdap/dns.json` | which RDAP server runs a TLD | Openly published file. The IANA terms page we tried returned 404, so no terms text was confirmed. | one request per run; cached 30 days |
| RDAP servers of the registries | registrar, IANA registrar id, creation date | Each registry sets its own policy. No key is needed. | 1 request/s per host; retry 429/5xx with backoff, honouring `Retry-After`; cached 30 days |
| Google Public DNS, `dns.google/resolve` (DoH JSON) | A, NS, MX | The Google Public DNS terms bind the use to the Google APIs Terms of Service. The page we read gives no numeric limit. The resolver makes its own queries to the domain's nameservers; the cloud VM does not. | 1 request/s; A, NS and MX per domain; cached 1 day |
| crt.sh, `crt.sh/?q=…&output=json` | issuers, earliest `not_before`, other names on the same certificates | The page states no terms of use or limits. It returns 429 and 502 under load. | 1 request per 5 s; 150 s timeout; 3 retries; cached 7 days |
| CERT Polska warning list, `hole.cert.pl/domains/v2/domains.json` (about 22 MB) | `cert_pl_listed`, listed date | Usage rules on cert.pl/lista-ostrzezen: anyone may use the list for any purpose, commercial use included; no attribution is required; no warranty; users act at their own risk. No download limit is stated. | one download per run, matched locally; each domain cached 1 day |

Failures never raise. A source that fails three times in a row is skipped for the rest of the
run, and its cached answers are used, labelled `stale-cache`. The shared User-Agent and pacing
are in `src/ftf/enrich/http.py`; the service policies are in `src/ftf/enrich/sources.py`.
