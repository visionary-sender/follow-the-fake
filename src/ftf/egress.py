"""Which network egress a collection run used (collector data: it describes us, never the scam).

Egress worker VMs (`scripts/egress.sh`) write `/etc/ftf/egress.json` when they start and after
every IP rotation:

    {"name": "ftf-egress-ec2-a1", "region": "europe-central2", "zone": "europe-central2-a",
     "ip_hash": "sha256:0f3c…"}

`ip_hash` is a salted SHA-256 of the external IP. The salt lives only in the VM's metadata, so the
hash cannot be turned back into the IP once the VM is gone, but two captures made from the same IP
in one rollout share a hash. The raw IP is never written.

On any other machine (the sandbox, the container) the file is absent and `current()`
returns None, so default behaviour is unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

DEFAULT_FILE = "/etc/ftf/egress.json"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_HASH = re.compile(r"sha256:[0-9a-f]{16,64}")


def egress_file() -> Path:
    return Path(os.environ.get("FTF_EGRESS_FILE") or DEFAULT_FILE)


def current(path: str | Path | None = None) -> dict | None:
    """The egress record for this machine, or None when there is none or it is malformed."""
    p = Path(path) if path is not None else egress_file()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key in ("name", "region", "zone"):
        v = raw.get(key)
        if not isinstance(v, str) or not _NAME.fullmatch(v):
            return None
        out[key] = v
    h = raw.get("ip_hash")
    if not isinstance(h, str) or not _HASH.fullmatch(h):
        return None
    out["ip_hash"] = h
    return out


def label(record: dict) -> str:
    """Short text form, e.g. `europe-central2-a ip sha256:0f3c…` (used as an indicator value).

    A record may also carry `proxy` (host:port of the upstream proxy, never its credentials)."""
    parts = []
    if record.get("zone"):
        parts.append(f"{record['zone']} ip {record['ip_hash']}")
    if record.get("proxy"):
        parts.append(f"via proxy {record['proxy']}")
    return ", ".join(parts)


# -- phase 2: an optional upstream proxy (e.g. a rented residential pool) ---------------------
#
# Off unless a proxy file exists. The file is owned by root, group `ftf`, mode 0640, so the
# unprivileged fetcher user can read it and nobody else can, and it is never in git:
#
#     FTF_PROXY_URL=http://<user>:<password>@<host>:<port>
#
# Path: $FTF_PROXY_FILE, else /etc/ftf/proxy.env. Only `http://` proxies that support CONNECT are
# accepted. Credentials never leave this object: `label` (host:port) is what gets recorded.

DEFAULT_PROXY_FILE = "/etc/ftf/proxy.env"


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def authorization(self) -> str | None:
        """Value of a Proxy-Authorization header, or None without credentials."""
        if self.username is None:
            return None
        raw = f"{self.username}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def url(self) -> str:
        """Proxy URL with credentials, for clients that take one (urllib, Playwright). Never log it."""
        if self.username is None:
            return f"http://{self.label}"
        return f"http://{quote(self.username, safe='')}:{quote(self.password or '', safe='')}@{self.label}"


def parse_proxy_url(value: str) -> Upstream | None:
    try:
        p = urlsplit(value.strip())
        port = p.port
    except ValueError:
        return None
    if p.scheme != "http" or not p.hostname or port is None or p.path not in ("", "/") or p.query or p.fragment:
        return None
    user = unquote(p.username) if p.username is not None else None
    pw = unquote(p.password) if p.password is not None else None
    return Upstream(p.hostname, port, user, pw)


def upstream_proxy(path: str | Path | None = None) -> Upstream | None:
    """The configured upstream proxy, or None (the default: connect directly)."""
    p = Path(path) if path is not None else Path(os.environ.get("FTF_PROXY_FILE") or DEFAULT_PROXY_FILE)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:  # present but unreadable: fail, never fall back to a direct connection
        raise ValueError(f"{p}: cannot read the proxy file ({type(e).__name__})") from None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "FTF_PROXY_URL":
            up = parse_proxy_url(value.strip().strip("'\""))
            if up is None:
                raise ValueError(f"{p}: FTF_PROXY_URL must look like http://user:password@host:port")
            return up
    return None
