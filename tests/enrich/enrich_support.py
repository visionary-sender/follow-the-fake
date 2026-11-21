"""Shared test helpers: recorded answers replayed by exact URL. No test here touches the network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ftf.enrich.http import PoliteClient, Response, Unavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "enrich"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
LOOKUP_HOSTS = {"data.iana.org", "rdap.verisign.com", "dns.google", "crt.sh", "hole.cert.pl"}

DOH_NAME = "https://dns.google/resolve?name=example.com&type="


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def example_routes() -> dict[str, bytes | int]:
    """Answers recorded on the workbench for example.com (a reserved domain), plus the list head."""
    return {
        "https://data.iana.org/rdap/dns.json": fixture("iana.json"),
        "https://rdap.verisign.com/com/v1/domain/example.com": fixture("rdap-example.json"),
        DOH_NAME + "A": fixture("doh-example-A.json"),
        DOH_NAME + "NS": fixture("doh-example-NS.json"),
        DOH_NAME + "MX": fixture("doh-example-MX.json"),
        "https://crt.sh/?q=example.com&output=json": fixture("crt-example.json"),
        "https://hole.cert.pl/domains/v2/domains.json": fixture("certpl-head40.json"),
    }


class FakeNet:
    """Replays answers by exact URL. A URL without a route raises Unavailable, as if unreachable."""

    def __init__(self, routes: dict[str, bytes | int] | None = None, *, down: bool = False) -> None:
        self.routes: dict[str, bytes | int] = dict(example_routes() if routes is None else routes)
        self.down = down
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
        self.calls.append(url)
        if self.down:
            raise Unavailable("network down (test)")
        answer = self.routes.get(url)
        if answer is None:
            raise Unavailable(f"no recorded answer for {url}")
        if isinstance(answer, int):
            return Response(answer, b"")
        return Response(200, answer)

    def hosts(self) -> set[str]:
        return {urlsplit(url).netloc for url in self.calls}


def client_for(net: FakeNet) -> PoliteClient:
    """Client with no real waiting: the fake clock never advances and sleep returns at once."""
    return PoliteClient(net, clock=lambda: 0.0, sleep=lambda seconds: None)
