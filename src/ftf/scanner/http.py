"""Small, polite HTTP client (standard library only) for the keyless library sources.

Only public library endpoints and creative image CDNs go through it; the scanner never fetches
a landing page (that is the sandboxed URL fetcher's job).
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ftf import __version__

USER_AGENT = f"Mozilla/5.0 (compatible; ftf-scanner/{__version__})"


class FetchError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    url: str
    content_type: str
    body: bytes


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise FetchError(f"refusing redirect to non-https URL {newurl[:100]}", code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirects)


class Client:
    """Sequential client with a minimum delay between requests and a hard size cap."""

    def __init__(self, delay: float = 2.0, timeout: float = 30.0, max_bytes: int = 64 * 1024 * 1024):
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.requests = 0
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if self._last and gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def request(
        self,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[dict] = None,
        max_bytes: Optional[int] = None,
    ) -> Response:
        if not url.lower().startswith("https://"):
            raise FetchError(f"refusing non-https URL {url[:100]}")
        self._wait()
        self.requests += 1
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        limit = max_bytes or self.max_bytes
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                body = resp.read(limit + 1)
                if len(body) > limit:
                    raise FetchError(f"response larger than {limit} bytes: {url[:100]}")
                return Response(resp.status, resp.geturl(), resp.headers.get("Content-Type", ""), body)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code} for {url[:100]}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"{type(exc).__name__}: {exc} for {url[:100]}") from None
