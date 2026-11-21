"""Phase-2 hook: the guard chained through an upstream CONNECT proxy (local fake proxy, no network)."""

from __future__ import annotations

import ipaddress
import socket
import socketserver
import threading

import pytest

from ftf import egress
from ftf.fetch.guard import GuardProxy

LOCAL = [ipaddress.ip_network("127.0.0.1/32")]


class _FakeUpstream(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, refuse: bool = False):
        self.requests: list[bytes] = []
        self.refuse = refuse
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _UpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                return
            head += chunk
        self.server.requests.append(head)
        if self.server.refuse:
            s.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            return
        target = head.split(b" ")[1].decode()
        host, port = target.rsplit(":", 1)
        up = socket.create_connection((host.strip("[]"), int(port)), timeout=10)
        s.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pump(a, b):
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(up, s), daemon=True)
        t.start()
        pump(s, up)
        t.join(10)
        up.close()


def _get(proxy, url):
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as s:
        s.sendall(f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


@pytest.fixture
def upstream():
    srv = _FakeUpstream()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_guard_goes_through_upstream_with_auth(server, upstream):
    up = egress.Upstream("127.0.0.1", upstream.server_address[1], "user", "p@ss")
    with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port], upstream=up) as proxy:
        resp = _get(proxy, server.url("/ok"))
    assert b"Breaking" in resp
    assert len(upstream.requests) == 1
    req = upstream.requests[0]
    # CONNECT to the checked IP, never the name: the proxy cannot be steered elsewhere.
    assert req.startswith(f"CONNECT 127.0.0.1:{server.port} HTTP/1.1".encode())
    assert up.authorization().encode() in req


def test_policy_still_applies_before_the_upstream(server, upstream):
    up = egress.Upstream("127.0.0.1", upstream.server_address[1])
    with GuardProxy(allow_ports=[server.port], upstream=up) as proxy:  # loopback not allowed
        resp = _get(proxy, server.url("/ok"))
    assert b" 403 " in resp.split(b"\r\n", 1)[0]
    assert upstream.requests == []


def test_upstream_refusal_is_a_connect_failure(server):
    srv = _FakeUpstream(refuse=True)
    try:
        up = egress.Upstream("127.0.0.1", srv.server_address[1], "u", "bad")
        with GuardProxy(allow_networks=LOCAL, allow_ports=[server.port], upstream=up) as proxy:
            _get(proxy, server.url("/ok"))
        assert proxy.events[-1].outcome == "connect_failed"
        assert "407" in proxy.events[-1].detail and "bad" not in proxy.events[-1].detail
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("value,ok", [
    ("http://u:p@proxy.example:8080", True),
    ("http://proxy.example:3128", True),
    ("https://u:p@proxy.example:8080", False),
    ("socks5://proxy.example:1080", False),
    ("http://proxy.example", False),
    ("http://u:p@proxy.example:8080/path", False),
])
def test_parse_proxy_url(value, ok):
    assert (egress.parse_proxy_url(value) is not None) is ok


def test_proxy_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_PROXY_FILE", str(tmp_path / "absent"))
    assert egress.upstream_proxy() is None
    f = tmp_path / "proxy.env"
    f.write_text("# vendor pool\nFTF_PROXY_URL='http://us%40er:s3cr%3At@pl.proxy.example:7000'\n")
    up = egress.upstream_proxy(f)
    assert (up.host, up.port, up.username, up.password) == ("pl.proxy.example", 7000, "us@er", "s3cr:t")
    assert up.label == "pl.proxy.example:7000" and "s3cr" not in repr(up)
    assert egress.parse_proxy_url(up.url()) == up
    f.write_text("FTF_PROXY_URL=ftp://x\n")
    with pytest.raises(ValueError):
        egress.upstream_proxy(f)


def test_malformed_proxy_file_fails_the_visit(tmp_path, monkeypatch):
    from ftf.fetch.browser import fetch_url

    f = tmp_path / "proxy.env"
    f.write_text("FTF_PROXY_URL=socks5://x:1\n")
    monkeypatch.setenv("FTF_PROXY_FILE", str(f))
    monkeypatch.setenv("FTF_EGRESS_FILE", str(tmp_path / "absent"))
    m = fetch_url("https://site.example/", tmp_path / "out")
    assert m["outcome"] == "fetcher_error" and "FTF_PROXY_URL" in m["reason"]


def test_scanner_client_uses_the_proxy(tmp_path, monkeypatch):
    import urllib.request

    from ftf.scanner import meta
    from ftf.scanner.http import Client

    monkeypatch.setenv("FTF_PROXY_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("https_proxy", "http://env-proxy.example:9")
    # Direct: no proxy handler is installed, and the environment's proxy variables are ignored.
    assert not [h for h in Client()._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert meta._proxy_option() == {}
    f = tmp_path / "proxy.env"
    f.write_text("FTF_PROXY_URL=http://u:p@pl.proxy.example:7000\n")
    monkeypatch.setenv("FTF_PROXY_FILE", str(f))
    handlers = [h for h in Client()._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert handlers[0].proxies["https"] == "http://u:p@pl.proxy.example:7000"
    assert meta._proxy_option() == {"proxy": {"server": "http://pl.proxy.example:7000", "username": "u", "password": "p"}}
