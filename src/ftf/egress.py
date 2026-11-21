"""Which network egress a collection run used (collector data: it describes us, never the scam).

Egress worker VMs (`scripts/egress.sh`) write `/etc/ftf/egress.json` when they start and after
every IP rotation:

    {"name": "ftf-egress-ec2-a1", "region": "europe-central2", "zone": "europe-central2-a",
     "ip_hash": "sha256:0f3c…"}

`ip_hash` is a salted SHA-256 of the external IP. The salt lives only in the VM's metadata, so the
hash cannot be turned back into the IP once the VM is gone, but two captures made from the same IP
in one rollout share a hash. The raw IP is never written.

On any other machine (the sandbox, the jury's container) the file is absent and `current()`
returns None, so default behaviour is unchanged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

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
    """Short text form, e.g. `europe-central2-a ip sha256:0f3c…` (used as an indicator value)."""
    return f"{record['zone']} ip {record['ip_hash']}"
