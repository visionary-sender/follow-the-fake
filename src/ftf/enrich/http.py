"""Polite HTTP GET for the keyless lookup services.

Standard library only. Every request carries one generic User-Agent (no personal data), each
host gets a minimum interval between requests, and 429 and 5xx answers are retried with
backoff (honouring Retry-After up to a cap). The transport is injectable, so tests replay
recorded answers without a network.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

USER_AGENT = "ftf-domain-enrichment/0.1 (+keyless registry lookups; research)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 120.0
DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class Unavailable(Exception):
    """A lookup gave no usable answer: network error, timeout, persistent 429/5xx, or a bad body."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True)
class Politeness:
    """Pacing for one service. `min_interval` is seconds between requests to the same host."""

    min_interval: float
    timeout: float
    retries: int
    backoff: float


FetchFn = Callable[[str, Mapping[str, str], float, int], Response]


def urllib_fetch(url: str, headers: Mapping[str, str], timeout: float, max_bytes: int) -> Response:
    """One GET with the standard library. Network failures raise `Unavailable`; HTTP errors are answers."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - https lookup URLs only
            status, raw_headers, body = resp.status, resp.headers, resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        status, raw_headers = exc.code, exc.headers
        try:
            body = exc.read(max_bytes + 1)
        except (OSError, http.client.HTTPException):
            body = b""
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise Unavailable(f"network error ({exc.__class__.__name__})") from exc
    headers_out = {k.lower(): v for k, v in (raw_headers.items() if raw_headers else [])}
    truncated = len(body) > max_bytes
    return Response(status, body[:max_bytes], headers_out, truncated)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return None


class PoliteClient:
    """GET with pacing, a shared User-Agent and retries. Callers decide what a status means."""

    def __init__(
        self,
        fetch: FetchFn | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._fetch = fetch or urllib_fetch
        self._clock = clock
        self._sleep = sleep
        self._user_agent = user_agent
        self._last: dict[str, float] = {}
        self.requests: Counter[str] = Counter()  # requests sent per host, for the run summary

    def get(
        self,
        url: str,
        policy: Politeness,
        *,
        accept: str = "application/json",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> Response:
        """Return the final answer. Raises `Unavailable` if retries run out or the body is oversized."""
        host = urlsplit(url).netloc
        headers = {"User-Agent": self._user_agent, "Accept": accept}
        attempt = 0
        while True:
            self._pace(host, policy.min_interval)
            try:
                resp = self._fetch(url, headers, policy.timeout, max_bytes)
            except Unavailable as exc:
                failure, wait, resp = str(exc), None, None
            else:
                if resp.status in RETRY_STATUSES:
                    failure, wait = f"HTTP {resp.status}", _retry_after(resp.headers)
                else:
                    if resp.truncated:
                        raise Unavailable(f"{host}: answer larger than {max_bytes} bytes")
                    return resp
            if attempt >= policy.retries:
                raise Unavailable(f"{host}: {failure} after {attempt + 1} attempts")
            self._sleep(wait if wait is not None else policy.backoff * (2**attempt))
            attempt += 1

    def _pace(self, host: str, interval: float) -> None:
        last = self._last.get(host)
        if last is not None and interval > 0:
            remaining = interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()
        self.requests[host] += 1
