"""Network guard: address policy and the proxy itself (no browser needed)."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from ftf.fetch.guard import GUARD_HEADER, GuardProxy, address_allowed, host_allowed_by_name

LOCAL = [ipaddress.ip_network("127.0.0.1/32")]


@pytest.mark.parametrize(
    "ip,ok",
    [
        ("169.254.169.254", False),  # cloud metadata
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fc00::1", False),
        ("fd12:3456::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:169.254.169.254", False),
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 form of 169.254.169.254
        ("2002:a9fe:a9fe::1", False),  # 6to4 form of 169.254.169.254
        ("8.8.8.8", True),
        ("93.184.216.34", True),
        ("::ffff:8.8.8.8", True),
        ("2001:4860:4860::8888", True),
    ],
)
def test_address_policy(ip, ok):
    assert address_allowed(ipaddress.ip_address(ip)) is ok


def test_allow_list_is_explicit():
    assert address_allowed(ipaddress.ip_address("127.0.0.1"), LOCAL)
    assert not address_allowed(ipaddress.ip_address("127.0.0.2"), LOCAL)


@pytest.mark.parametrize(
    "host,ok",
    [
        ("metadata.google.internal", False),
        ("METADATA.GOOGLE.INTERNAL.", False),
        ("metadata", False),
        ("localhost", False),
        ("foo.localhost", False),
        ("printer.local", False),
        ("example.com", True),
        ("landing-page.example", True),
    ],
)
def test_name_policy(host, ok):
    assert host_allowed_by_name(host) is ok


def _raw(proxy: GuardProxy, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as s:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def _status(resp: bytes) -> int:
    return int(resp.split(b" ", 2)[1])


@pytest.mark.parametrize(
    "request_line,guard",
    [
        (b"GET http://169.254.169.254/computeMetadata/v1/ HTTP/1.1", b"blocked_address"),
        (b"CONNECT 169.254.169.254:443 HTTP/1.1", b"blocked_address"),
        (b"GET http://metadata.google.internal/ HTTP/1.1", b"blocked_name"),
        (b"CONNECT [::1]:443 HTTP/1.1", b"blocked_address"),
        (b"GET http://10.1.2.3:8080/ HTTP/1.1", b"blocked_address"),
        (b"CONNECT 8.8.8.8:25 HTTP/1.1", b"blocked_port"),
        (b"GET http://nonexistent.invalid/ HTTP/1.1", b"dns_failure"),
    ],
)
def test_proxy_refuses(request_line, guard):
    with GuardProxy() as proxy:
        resp = _raw(proxy, request_line + b"\r\nHost: x\r\n\r\n")
    assert _status(resp) in (403, 502)
    assert proxy.header.startswith(GUARD_HEADER + "-") and len(proxy.header) > len(GUARD_HEADER) + 8
    assert f"{proxy.header}: ".encode() + guard in resp
    assert any(e.outcome == guard.decode() for e in proxy.events)


def test_loopback_blocked_by_default(server):
    with GuardProxy(allow_ports=[server.port]) as proxy:
        resp = _raw(proxy, f"GET {server.url('/ok')} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert _status(resp) == 403
    assert ("GET", "/ok") not in server.hits


def test_forwards_when_allowed(server):
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port]) as proxy:
        resp = _raw(proxy, f"GET {server.url('/ok')} HTTP/1.1\r\nHost: x\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
    assert _status(resp) == 200
    assert b"Breaking" in resp
    assert proxy.bytes_in > 0


def test_upstream_hop_headers_and_fake_guard_header_are_stripped(server):
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port]) as proxy:
        resp = _raw(proxy, f"GET {server.url('/spoof')} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    head = resp.split(b"\r\n\r\n", 1)[0].lower()
    assert _status(resp) == 200
    assert b"connection: close" in head and b"keep-alive" not in head
    assert b"x-ftf-guard" not in head
    assert b"Real page" in resp


def test_unique_header_per_proxy():
    with GuardProxy() as a, GuardProxy() as b:
        assert a.header != b.header


def test_tunnel_when_allowed(server):
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port]) as proxy:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as s:
            s.sendall(f"CONNECT 127.0.0.1:{server.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in s.recv(1024)
            s.sendall(b"GET /ok HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := s.recv(65536):
                data += chunk
    assert b"Breaking" in data


def test_mixed_dns_answer_is_refused():
    """A name that resolves to a public and a private address is refused outright."""

    async def resolver(host, port):
        return ["93.184.216.34", "169.254.169.254"]

    with GuardProxy(resolver=resolver) as proxy:
        resp = _raw(proxy, b"GET http://rebind.example/ HTTP/1.1\r\nHost: x\r\n\r\n")
    assert _status(resp) == 403
    assert proxy.events[-1].outcome == "blocked_address"


def test_connects_to_the_checked_address(server):
    """DNS rebinding: the proxy connects to the address it validated, never re-resolves."""
    answers = iter([["127.0.0.1"], ["169.254.169.254"]])

    async def resolver(host, port):
        return next(answers)

    with GuardProxy(resolver=resolver, allow_networks=LOCAL, allow_ports=[server.port]) as proxy:
        resp = _raw(proxy, f"GET http://rebind.example:{server.port}/ok HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert _status(resp) == 200
    ok = [e for e in proxy.events if e.outcome == "ok"]
    assert [e.address for e in ok] == ["127.0.0.1"]


def test_byte_budget(server):
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port], max_bytes=100_000) as proxy:
        resp = _raw(proxy, f"GET {server.url('/big')} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert len(resp) < 200_000
        assert proxy.budget_exceeded
        again = _raw(proxy, f"GET {server.url('/ok')} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert _status(again) == 509


def test_rejects_chunked_upload(server):
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port]) as proxy:
        resp = _raw(proxy, f"POST {server.url('/ok')} HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n".encode())
    assert _status(resp) == 501
