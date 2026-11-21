"""Stop-list: public values that must never count as evidence that two scams are related.

`is_stopped(indicator)` is called by `ftf.indicators.extract_all` on every extracted indicator; a
stopped indicator is dropped. The entries live in `stoplist.json` next to this module (shipped as
package data). Extractors are written in parallel and choose their own `kind` names, so matching
keys on kind *families*, recognised from the kind string:

| family    | kind looks like                                       | value matched against                |
|-----------|-------------------------------------------------------|--------------------------------------|
| host      | `host`/`domain`, or ends in `_host`, `_hostname`, `_domain` | `hosts.*` (entry or any subdomain), `bare_domains` (exact) |
| url       | `url`, or ends in `_url`/`_uri`, or contains `src`/`href` | the URL's host against `hosts.cdn` and `hosts.brands` only |
| ip        | `ip`, or ends in `_ip`/`_ip_address`                  | `ip_networks` (CIDR)                  |
| hash      | contains `sha256`, `sha1`, `md5` or `hash`            | `hashes` (hex, optional `sha256:` prefix) |
| file name | contains `basename`, `filename`, `file_name`, `asset_name` | `file_names` regexes (`.pobrane` removed) |
| header    | contains `header`                                     | `values.headers` (exact, case-insensitive) |
| parameter | contains `param` or `query`                           | `values.params` (exact, case-insensitive) |

Hosts matched on URL kinds are restricted to CDNs and brand sites: an SDK host such as
www.facebook.com also serves scammers' own pages, so a URL there can be a real identifier.

Never stopped, whatever the value:
- the collector layer (it is excluded from linking by type already, and the demo shows it);
- kinds naming an account-like identifier: anything containing `pixel`, `tenant`, `owner`,
  `account`, `advertiser`, `campaign`, `gateway`, `token` or `_id`. A Meta pixel id or a platform
  tenant id is a scam identifier even when it sits on a public host.
Perceptual hashes (`phash`, `dhash`, `ahash`) are not exact content hashes and are left alone.
"""

from __future__ import annotations

import ipaddress
import json
import re
from functools import lru_cache
from importlib import resources
from urllib.parse import urlsplit

from ftf.contract import Indicator, Layer

_PROTECTED = ("pixel", "tenant", "owner", "account", "advertiser", "campaign", "gateway", "token", "_id")
_PERCEPTUAL = ("phash", "dhash", "ahash", "whash", "perceptual")
_HEX = re.compile(r"^(?:sha256:|sha1:|md5:)?([0-9a-f]{32,64})$")


class _Lists:
    def __init__(self, data: dict) -> None:
        hosts = data["hosts"]
        self.all_hosts = frozenset(h.lower() for key, vals in hosts.items() if key != "about" for h in vals)
        self.url_hosts = frozenset(h.lower() for h in hosts.get("cdn", []) + hosts.get("brands", []))
        self.bare = frozenset(d.lower() for d in data["bare_domains"]["values"])
        self.nets = tuple(ipaddress.ip_network(n) for n in data["ip_networks"]["values"])
        self.hashes = frozenset(h.lower() for h in data["hashes"]["values"])
        self.names = re.compile("|".join(f"(?:{p})" for p in data["file_names"]["values"]), re.I)
        self.headers = frozenset(v.lower() for v in data["values"]["headers"])
        self.params = frozenset(v.lower() for v in data["values"]["params"])


@lru_cache(maxsize=1)
def lists() -> _Lists:
    text = resources.files("ftf.indicators").joinpath("stoplist.json").read_text(encoding="utf-8")
    return _Lists(json.loads(text))


def family(kind: str) -> str | None:
    """The stop-list family of an indicator kind, or None if the stop-list does not apply."""
    k = kind.lower()
    if any(p in k for p in _PROTECTED) or any(p in k for p in _PERCEPTUAL):
        return None
    if k in ("host", "domain", "hostname") or k.endswith(("_host", "_hostname", "_domain")):
        return "host"
    if k == "url" or k.endswith(("_url", "_uri")) or "src" in k or "href" in k:
        return "url"
    if k == "ip" or k.endswith(("_ip", "_ip_address")):
        return "ip"
    if any(p in k for p in ("sha256", "sha1", "md5", "hash")):
        return "hash"
    if any(p in k for p in ("basename", "filename", "file_name", "asset_name")):
        return "name"
    if "header" in k:
        return "header"
    if "param" in k or "query" in k:
        return "param"
    return None


def _norm_host(value: str) -> str:
    v = value.strip().lower()
    if "://" in v:
        v = urlsplit(v).hostname or ""
    v = v.split("/", 1)[0]
    if v.startswith("[") or v.count(":") > 1:
        return v.strip("[]")
    return v.split(":", 1)[0].rstrip(".")


def host_stopped(host: str, entries: frozenset[str]) -> bool:
    parts = host.split(".")
    return any(".".join(parts[i:]) in entries for i in range(len(parts) - 1))


def is_stopped(indicator: Indicator) -> bool:
    """True if the indicator's value is a public library, CDN, shared front end or brand asset."""
    if indicator.layer is Layer.collector or indicator.value is None:
        return False
    fam = family(indicator.kind)
    if fam is None:
        return False
    L = lists()
    value = indicator.value.strip()
    if fam == "host":
        host = _norm_host(value)
        return bool(host) and (host in L.bare or host_stopped(host, L.all_hosts))
    if fam == "url":
        try:
            host = (urlsplit(value if "://" in value else "//" + value).hostname or "").lower()
        except ValueError:
            return False
        return bool(host) and host_stopped(host, L.url_hosts)
    if fam == "ip":
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(ip in net for net in L.nets if net.version == ip.version)
    if fam == "hash":
        m = _HEX.match(value.lower())
        return bool(m) and m.group(1) in L.hashes
    if fam == "name":
        name = value.replace("\\", "/").rsplit("/", 1)[-1].split("?", 1)[0]
        if name.lower().endswith(".pobrane"):
            name = name[: -len(".pobrane")]
        return bool(L.names.fullmatch(name))
    if fam == "header":
        return value.lower() in L.headers
    if fam == "param":
        return value.lower() in L.params
    return False
