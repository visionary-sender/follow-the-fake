"""Start probe and short lookup timeouts for online-by-default runs (HACK-91).

`network_up()` asks one DNS-over-HTTPS question about example.com with a short timeout. When it
fails, the run is offline: enrichment reads the cache and the precomputed base, as with `--offline`.
`ShortTimeoutClient` caps each lookup's timeout and retries, so a dead network costs seconds, not
minutes. The Enricher itself is unchanged; it already falls back to the cache when a lookup fails.
"""

from __future__ import annotations

from dataclasses import replace

from ftf.enrich.http import FetchFn, PoliteClient, Politeness, Unavailable
from ftf.enrich.sources import DOH_URL

LOOKUP_TIMEOUT = 5.0  # seconds per request
PROBE_URL = f"{DOH_URL}?name=example.com&type=A"
PROBE_POLICY = Politeness(min_interval=0.0, timeout=LOOKUP_TIMEOUT, retries=0, backoff=0.0)


class ShortTimeoutClient(PoliteClient):
    """PoliteClient whose every lookup times out after LOOKUP_TIMEOUT and retries at most once."""

    def get(self, url: str, policy: Politeness, **kwargs):  # type: ignore[override]
        short = replace(
            policy,
            timeout=min(policy.timeout, LOOKUP_TIMEOUT),
            retries=min(policy.retries, 1),
            backoff=min(policy.backoff, 1.0),
        )
        return super().get(url, short, **kwargs)


def network_up(fetch: FetchFn | None = None) -> bool:
    """True if the probe gets an HTTP 200 within LOOKUP_TIMEOUT; any failure means no network."""
    try:
        resp = ShortTimeoutClient(fetch).get(PROBE_URL, PROBE_POLICY, accept="application/dns-json")
    except Unavailable:
        return False
    return resp.status == 200
