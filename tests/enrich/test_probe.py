"""Start probe and short lookup timeouts (HACK-91). No network: every fetch is injected or refused."""

import pytest

from ftf.enrich.http import Politeness, Response, Unavailable
from ftf.enrich.probe import LOOKUP_TIMEOUT, PROBE_URL, ShortTimeoutClient, network_up


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
    assert seen == [(PROBE_URL, LOOKUP_TIMEOUT)]


@pytest.mark.parametrize("result", [Unavailable("network error (URLError)"), Response(503, b"", {}), Response(429, b"", {})])
def test_probe_is_down_on_failure(result):
    assert network_up(_fetch(result, [])) is False


def test_probe_is_down_without_network():
    assert network_up() is False  # the conftest refuses the network: no real request is made


def test_short_timeout_caps_each_lookup_and_retries_once():
    seen, slept = [], []
    client = ShortTimeoutClient(_fetch(Unavailable("timed out"), seen), sleep=slept.append)
    with pytest.raises(Unavailable):
        client.get("https://crt.sh/?q=example.com", Politeness(min_interval=0.0, timeout=150, retries=3, backoff=15))
    assert [timeout for _, timeout in seen] == [LOOKUP_TIMEOUT, LOOKUP_TIMEOUT]
    assert slept == [1.0]
