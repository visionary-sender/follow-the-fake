"""Start probe and short lookup timeouts. No network: every fetch is injected or refused."""

import pytest

from ftf.enrich.http import Politeness, Response, Unavailable
from ftf.enrich.probe import HOST_TIMEOUT, LOOKUP_TIMEOUT, PROBE_URLS, ShortTimeoutClient, network_up


def _fetch(result, seen):
    def fetch(url, headers, timeout, max_bytes):
        seen.append((url, timeout))
        if isinstance(result, Exception):
            raise result
        return result

    return fetch


def test_probe_is_up_on_http_200():
    seen = []
    assert network_up(_fetch(Response(200, b"{}", {}), seen)) is True
    assert seen == [(PROBE_URLS[0], LOOKUP_TIMEOUT)]


def test_probe_falls_back_to_the_second_host():
    seen = []

    def fetch(url, headers, timeout, max_bytes):
        seen.append(url)
        if "dns.google" in url:
            raise Unavailable("timed out")
        return Response(200, b"{}", {})

    assert network_up(fetch) is True
    assert seen == list(PROBE_URLS)


@pytest.mark.parametrize("result", [Unavailable("network error (URLError)"), Response(503, b"", {}), Response(429, b"", {})])
def test_probe_is_down_on_failure(result):
    seen = []
    assert network_up(_fetch(result, seen)) is False
    assert len(seen) == len(PROBE_URLS)


def test_probe_is_down_without_network():
    assert network_up() is False  # the conftest refuses the network: no real request is made


def test_short_timeout_caps_each_lookup_and_retries_once():
    seen, slept = [], []
    client = ShortTimeoutClient(_fetch(Unavailable("timed out"), seen), sleep=slept.append)
    with pytest.raises(Unavailable):
        client.get("https://crt.sh/?q=example.com", Politeness(min_interval=0.0, timeout=150, retries=3, backoff=15))
    # one request reaches the network; its retry is refused because the first attempt got no answer
    assert [timeout for _, timeout in seen] == [HOST_TIMEOUT["crt.sh"]]
    assert slept == [1.0]


def test_transport_failure_ends_network_use_for_the_run():
    seen = []
    client = ShortTimeoutClient(_fetch(Unavailable("timed out"), seen), sleep=lambda s: None)
    policy = Politeness(min_interval=0.0, timeout=30, retries=2, backoff=5)
    with pytest.raises(Unavailable):
        client.get("https://rdap.org/domain/a.example", policy)
    with pytest.raises(Unavailable, match="earlier in this run"):
        client.get("https://crt.sh/?q=a.example", policy)
    assert len(seen) == 1  # the first request only; later lookups never reach the network
