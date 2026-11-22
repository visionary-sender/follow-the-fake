"""Network guard: a small forward proxy that every browser request goes through.

Chromium is started with `--proxy-server` pointing here, so it never resolves or connects to a
host itself. For each request the proxy:

1. refuses ports outside the web range and hostnames that are internal by name
   (`localhost`, `*.internal`, `metadata.google.internal`, ...);
2. resolves the host once and refuses it if **any** returned address is private, loopback,
   link-local, multicast, reserved or otherwise not globally routable (this covers
   169.254.169.254, RFC 1918, 100.64/10, ::1, fc00::/7, IPv4-mapped/6to4/Teredo/NAT64 forms);
3. connects to the exact address it checked, so a DNS answer that changes between the check
   and the connect (DNS rebinding) cannot redirect the connection.

Redirects need no special handling: every hop is a new request through the proxy. The proxy
also enforces a byte budget for the whole page visit. Every decision is recorded as a
`GuardEvent`, which the fetcher uses to explain dead pages (DNS failure, refused, ...).
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Prefix of the header the proxy puts on its own refusals. Each proxy adds a random suffix, so a
# site cannot fake a refusal by sending the header itself.
GUARD_HEADER = "X-Ftf-Guard"
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"})

BLOCKED_NAMES = frozenset({"localhost", "metadata", "metadata.google.internal", "metadata.goog"})
BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".home.arpa", ".lan")
# Ports a web page may use. Everything else (SMTP, SSH, databases, ...) is refused.
ALLOWED_PORTS = frozenset({80, 443, 8000, 8008, 8080, 8443, 8888})

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def address_allowed(ip: IPAddress, allow: Iterable[IPNetwork] = ()) -> bool:
    """True if a browser may connect to `ip`. `allow` is a test-only escape hatch."""
    for net in allow:
        if ip.version == net.version and ip in net:
            return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[IPAddress] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _NAT64_LOCAL:
            return False
        if embedded and not all(address_allowed(e) for e in embedded):
            return False
        if ip.ipv4_mapped is not None:
            return True
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved


def host_allowed_by_name(host: str) -> bool:
    h = host.rstrip(".").lower()
    if not h or h in BLOCKED_NAMES or h.endswith(BLOCKED_SUFFIXES):
        return False
    return True


def parse_ip(host: str) -> IPAddress | None:
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None


@dataclass
class GuardEvent:
    host: str
    port: int
    outcome: str  # ok | blocked_name | blocked_port | blocked_address | dns_failure | connect_failed | budget_exceeded | bad_request
    detail: str = ""
    address: str | None = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "outcome": self.outcome, "detail": self.detail, "address": self.address}


class GuardProxy:
    """HTTP/HTTPS forward proxy on 127.0.0.1 that enforces the address policy.

    Runs its own asyncio loop in a daemon thread; use as a context manager.
    """

    def __init__(
        self,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        dns_timeout: float = 8.0,
        connect_timeout: float = 10.0,
        idle_timeout: float = 30.0,
        allow_networks: Iterable[IPNetwork] = (),
        allow_ports: Iterable[int] | None = None,
        resolver=None,
        upstream=None,
    ) -> None:
        self.max_bytes = max_bytes
        # Optional ftf.egress.Upstream: after the address checks, connect through this HTTP proxy
        # (CONNECT to the checked IP, so the proxy cannot be steered to another address).
        self.upstream = upstream
        self.dns_timeout = dns_timeout
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.allow_networks = tuple(allow_networks)
        self.allow_ports = frozenset(allow_ports) if allow_ports is not None else ALLOWED_PORTS
        # Optional async (host, port) -> list of address strings, used by tests in place of DNS.
        self.resolver = resolver
        self.header = f"{GUARD_HEADER}-{secrets.token_hex(8)}"
        self.events: list[GuardEvent] = []
        self.bytes_in = 0
        self.budget_exceeded = False
        self.port: int | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # -- lifecycle -------------------------------------------------------------------------

    def __enter__(self) -> "GuardProxy":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ftf-guard-proxy", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._start_error is not None:
            raise self._start_error
        if self.port is None:
            raise RuntimeError("guard proxy did not start")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._server = loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as e:  # noqa: BLE001 - reported to the starting thread
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._server.close()
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            try:
                loop.run_until_complete(asyncio.wait_for(self._server.wait_closed(), 5))
            except (asyncio.TimeoutError, OSError):
                pass
            loop.close()

    # -- bookkeeping -----------------------------------------------------------------------

    def _record(self, event: GuardEvent) -> GuardEvent:
        with self._lock:
            self.events.append(event)
        return event

    def events_for(self, host: str, port: int | None = None) -> list[GuardEvent]:
        h = host.rstrip(".").lower()
        with self._lock:
            return [e for e in self.events if e.host == h and (port is None or e.port == port)]

    def _account(self, n: int) -> bool:
        """Count received bytes; False once the visit's byte budget is used up."""
        with self._lock:
            self.bytes_in += n
            if self.bytes_in > self.max_bytes:
                self.budget_exceeded = True
                return False
            return True

    # -- policy ----------------------------------------------------------------------------

    async def resolve_checked(self, host: str, port: int) -> tuple[IPAddress | None, GuardEvent]:
        """Apply the policy to host:port; return the address to connect to (or None)."""
        h = host.rstrip(".").lower()
        if self.budget_exceeded:
            return None, self._record(GuardEvent(h, port, "budget_exceeded", f"over {self.max_bytes} bytes"))
        if port not in self.allow_ports:
            return None, self._record(GuardEvent(h, port, "blocked_port", f"port {port} not allowed"))
        literal = parse_ip(h)
        if literal is None and not host_allowed_by_name(h):
            return None, self._record(GuardEvent(h, port, "blocked_name", "internal host name"))
        if literal is not None:
            addrs = [literal]
        else:
            loop = asyncio.get_running_loop()
            try:
                if self.resolver is not None:
                    infos = [(None, None, None, None, (a, port)) for a in await self.resolver(h, port)]
                else:
                    infos = await asyncio.wait_for(
                        loop.getaddrinfo(h, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP), self.dns_timeout
                    )
            except asyncio.TimeoutError:
                return None, self._record(GuardEvent(h, port, "dns_failure", "DNS timeout"))
            except (socket.gaierror, UnicodeError, OSError) as e:
                return None, self._record(GuardEvent(h, port, "dns_failure", str(e)))
            addrs = []
            for info in infos:
                ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
                if ip not in addrs:
                    addrs.append(ip)
            if not addrs:
                return None, self._record(GuardEvent(h, port, "dns_failure", "no addresses"))
        bad = [a for a in addrs if not address_allowed(a, self.allow_networks)]
        if bad:
            return None, self._record(
                GuardEvent(h, port, "blocked_address", "non-public address " + ", ".join(map(str, bad)), str(bad[0]))
            )
        # Prefer IPv4: the sandbox and most containers have no IPv6 route.
        addrs.sort(key=lambda a: a.version)
        return addrs[0], GuardEvent(h, port, "ok", address=str(addrs[0]))

    # -- connection handling ---------------------------------------------------------------

    async def _reply(self, writer: asyncio.StreamWriter, status: int, text: str, outcome: str = "bad_request") -> None:
        body = f"ftf guard: {text}\n".encode()
        writer.write(
            f"HTTP/1.1 {status} Blocked\r\n{self.header}: {outcome}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.idle_timeout)
        if len(head) > 64 * 1024:
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return
        headers = [ln for ln in lines[1:] if ln]
        if method.upper() == "CONNECT":
            host, port = _split_hostport(target, 443)
            if host is None:
                await self._reply(writer, 400, "bad CONNECT target")
                return
            await self._tunnel(host, port, reader, writer)
            return
        if not target.lower().startswith("http://"):
            await self._reply(writer, 400, "only absolute http:// URLs are proxied")
            return
        rest = target[len("http://"):]
        hostport, _, path = rest.partition("/")
        hostport = hostport.rpartition("@")[2]  # never forward userinfo
        host, port = _split_hostport(hostport, 80)
        if host is None:
            await self._reply(writer, 400, "bad URL")
            return
        await self._forward(method, host, port, "/" + path, version, headers, reader, writer)

    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | GuardEvent:
        ip, event = await self.resolve_checked(host, port)
        if ip is None:
            return event
        try:
            if self.upstream is None:
                conn = await asyncio.wait_for(asyncio.open_connection(str(ip), port), self.connect_timeout)
            else:
                conn = await asyncio.wait_for(self._via_upstream(ip, port), self.connect_timeout)
        except (OSError, asyncio.TimeoutError, _UpstreamRefused) as e:
            return self._record(GuardEvent(event.host, port, "connect_failed", str(e) or type(e).__name__, str(ip)))
        self._record(event)
        return conn

    async def _via_upstream(self, ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        up = self.upstream
        r, w = await asyncio.open_connection(up.host, up.port)
        try:
            return await self._connect_handshake(r, w, ip, port)
        except BaseException:  # refusal, reset or the caller's timeout: never leak the socket
            w.close()
            raise

    async def _connect_handshake(self, r, w, ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        up = self.upstream
        target = f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        auth = up.authorization()
        if auth:
            lines.append(f"Proxy-Authorization: {auth}")
        w.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await w.drain()
        try:
            head = await r.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise _UpstreamRefused(f"upstream proxy {up.label}: no valid reply to CONNECT") from None
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = status.split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or parts[1] != "200":
            raise _UpstreamRefused(f"upstream proxy {up.label} refused CONNECT: {status[:80]}")
        return r, w

    async def _tunnel(self, host: str, port: int, reader, writer) -> None:
        opened = await self._open(host, port)
        if isinstance(opened, GuardEvent):
            await self._reply(writer, _status_for(opened), opened.outcome + ": " + opened.detail, opened.outcome)
            return
        up_r, up_w = opened
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe_both(reader, writer, up_r, up_w)

    async def _forward(self, method, host, port, path, version, headers, reader, writer) -> None:
        length = 0
        out_headers = []
        for h in headers:
            name, _, value = h.partition(":")
            lname = name.strip().lower()
            if lname in ("proxy-connection", "proxy-authorization", "connection", "keep-alive"):
                continue
            if lname == "transfer-encoding":
                await self._reply(writer, 501, "chunked request bodies are not proxied")
                return
            if lname == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    await self._reply(writer, 400, "bad Content-Length")
                    return
            out_headers.append(h)
        if length < 0 or length > 10 * 1024 * 1024:
            await self._reply(writer, 413, "request body too large")
            return
        opened = await self._open(host, port)
        if isinstance(opened, GuardEvent):
            await self._reply(writer, _status_for(opened), opened.outcome + ": " + opened.detail, opened.outcome)
            return
        up_r, up_w = opened
        try:
            req = f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in out_headers) + "Connection: close\r\n\r\n"
            up_w.write(req.encode("latin-1"))
            if length:
                up_w.write(await asyncio.wait_for(reader.readexactly(length), self.idle_timeout))
            await up_w.drain()
            # One request per client connection: the response head is rewritten to
            # `Connection: close`, the body streamed back and both sides closed, so the browser
            # never sends a second request on this connection (it could not reach this upstream).
            for _ in range(10):  # skip interim 1xx heads (e.g. 103 Early Hints) up to the final one
                head = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), self.idle_timeout)
                if len(head) > 64 * 1024 or not self._account(len(head)):
                    return
                lines = head.decode("latin-1").split("\r\n")
                try:
                    code = int(lines[0].split(" ", 2)[1])
                except (IndexError, ValueError):
                    code = 0
                if code == 101:  # protocol upgrade (e.g. WebSocket over plain http): not relayed
                    await self._reply(writer, 502, "protocol upgrade not supported by the guard")
                    return
                interim = 100 <= code < 200
                if not interim:
                    break
            else:
                return
            kept = [ln for ln in lines[1:] if ln and ln.partition(":")[0].strip().lower() not in HOP_BY_HOP
                    and not ln.lower().startswith(GUARD_HEADER.lower())]
            writer.write((lines[0] + "\r\n" + "".join(ln + "\r\n" for ln in kept) + "Connection: close\r\n\r\n").encode("latin-1"))
            await self._relay_body(method, lines, up_r, writer)
        finally:
            up_w.close()

    async def _relay_body(self, method: str, lines: list[str], up_r, writer) -> None:
        """Stream the response body and stop at its end, even if the upstream keeps the
        connection open despite our `Connection: close`."""
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError):
            status = 0
        fields = {}
        for ln in lines[1:]:
            name, _, value = ln.partition(":")
            fields[name.strip().lower()] = value.strip()
        if method.upper() == "HEAD" or status in (204, 304) or 100 <= status < 200:
            return
        if "chunked" in fields.get("transfer-encoding", "").lower():
            while True:
                size_line = await asyncio.wait_for(up_r.readuntil(b"\r\n"), self.idle_timeout)
                if not self._account(len(size_line)):
                    return
                writer.write(size_line)
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    return
                if size == 0:
                    while True:  # trailers, up to the empty line
                        trailer = await asyncio.wait_for(up_r.readuntil(b"\r\n"), self.idle_timeout)
                        writer.write(trailer)
                        if trailer == b"\r\n":
                            await writer.drain()
                            return
                await self._copy(up_r, writer, size + 2)
        elif "content-length" in fields:
            try:
                length = int(fields["content-length"])
            except ValueError:
                return
            await self._copy(up_r, writer, max(0, length))
        else:
            await self._pipe(up_r, writer, count=True)

    async def _copy(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, n: int) -> None:
        while n > 0:
            chunk = await asyncio.wait_for(src.read(min(n, 65536)), self.idle_timeout)
            if not chunk:
                return
            if not self._account(len(chunk)):
                self._record(GuardEvent("*", 0, "budget_exceeded", f"over {self.max_bytes} bytes"))
                raise ConnectionError("byte budget exceeded")
            n -= len(chunk)
            dst.write(chunk)
            await dst.drain()

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, count: bool) -> None:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), self.idle_timeout)
            if not chunk:
                return
            if count and not self._account(len(chunk)):
                self._record(GuardEvent("*", 0, "budget_exceeded", f"over {self.max_bytes} bytes"))
                return
            dst.write(chunk)
            await dst.drain()

    async def _pipe_both(self, c_r, c_w, u_r, u_w) -> None:
        up = asyncio.ensure_future(self._pipe(c_r, u_w, count=False))
        down = asyncio.ensure_future(self._pipe(u_r, c_w, count=True))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (up, down):
                t.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            u_w.close()


class _UpstreamRefused(Exception):
    pass


def _split_hostport(s: str, default_port: int) -> tuple[str | None, int]:
    s = s.strip()
    if not s:
        return None, default_port
    if s.startswith("["):
        end = s.find("]")
        if end < 0:
            return None, default_port
        host, rest = s[1:end], s[end + 1:]
        port_s = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_s = s.partition(":")
    if not host:
        return None, default_port
    try:
        port = int(port_s) if port_s else default_port
    except ValueError:
        return None, default_port
    if not 0 < port < 65536:
        return None, default_port
    return host.lower(), port


def _status_for(event: GuardEvent) -> int:
    return {"dns_failure": 502, "connect_failed": 502, "budget_exceeded": 509}.get(event.outcome, 403)


