"""Start probe and short lookup timeouts for online-by-default runs (HACK-91).

`network_up()` asks one DNS-over-HTTPS question about example.com, first at dns.google and then at
cloudflare-dns.com, each with a short timeout. When both fail, the run is offline: enrichment reads
the cache and the precomputed base, as with `--offline`.

`ShortTimeoutClient` caps each lookup's timeout (5 s; 15 s for crt.sh, which is slow), retries at
most once, and after the first transport failure (no HTTP answer at all) refuses further requests
for the rest of the run, so a dead network costs about one timeout, not one per lookup. urllib's
timeout does not bound DNS resolution, so a stalled resolver can take longer. The Enricher is unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from ftf.enrich.http import FetchFn, PoliteClient, Politeness, Unavailable, urllib_fetch
from ftf.enrich.sources import DOH_URL

LOOKUP_TIMEOUT = 5.0  # seconds per request
# crt.sh answers slowly under load. A decision: confirm on the jury network.
HOST_TIMEOUT = {"crt.sh": 15.0}
PROBE_URLS = (
    f"{DOH_URL}?name=example.com&type=A",
    "https://cloudflare-dns.com/dns-query?name=example.com&type=A",
)
PROBE_POLICY = Politeness(min_interval=0.0, timeout=LOOKUP_TIMEOUT, retries=0, backoff=0.0)


class ShortTimeoutClient(PoliteClient):
    """PoliteClient with capped timeouts and at most one retry; the run stays offline after a transport failure."""

    def __init__(self, fetch: FetchFn | None = None, **kwargs) -> None:
        inner = fetch or urllib_fetch
        self.down = False  # set by the first request that got no HTTP answer

        def guarded(url, headers, timeout, max_bytes):
            if self.down:
                raise Unavailable("network down earlier in this run")
            try:
                return inner(url, headers, timeout, max_bytes)
            except Unavailable:
                self.down = True
                raise

        super().__init__(guarded, **kwargs)

    def get(self, url: str, policy: Politeness, **kwargs):  # type: ignore[override]
        cap = HOST_TIMEOUT.get(urlsplit(url).netloc, LOOKUP_TIMEOUT)
        short = replace(
            policy,
            timeout=min(policy.timeout, cap),
            retries=min(policy.retries, 1),
            backoff=min(policy.backoff, 1.0),
        )
        return super().get(url, short, **kwargs)


def network_up(fetch: FetchFn | None = None) -> bool:
    """True if any probe host answers HTTP 200 within LOOKUP_TIMEOUT. Each host gets its own client."""
    for url in PROBE_URLS:
        try:
            resp = ShortTimeoutClient(fetch).get(url, PROBE_POLICY, accept="application/dns-json")
        except Unavailable:
            continue
        if resp.status == 200:
            return True
    return False
