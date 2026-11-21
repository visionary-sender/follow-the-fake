"""E1 hidden layer: hide the scammers' platform ids, then prove nothing of them is left.

`hide_platform_ids` removes every indicator of a hidden kind (all `window.aio` config fields and
the landing-cdn tenant/landing path segments), drops asset URLs that embed a landing-cdn path
(their content hashes stay), and masks any remaining occurrence of a hidden value (for example
the handle-key parameter name inside a URL parameter shape) as `{hidden}`. Platform constants
(SDK assets, action-files account, API endpoints) stay: they name the platform, not the tenant.

`leak_check` then searches every hidden value in every remaining indicator value,
case-insensitively. Values of 8+ characters match as substrings; shorter ones (the tenant `666`,
human campaign ids) only as whole tokens, because a 3-digit substring hits random hashes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ftf.contract import Dataset

# Indicator kinds that carry platform ids (the E1 ground truth). Nothing of these may stay visible.
HIDDEN_KINDS = frozenset({
    "aio_owner",
    "aio_campaign",
    "aio_campaign_uuid",
    "aio_cdn_tenant",
    "aio_landing",
    "aio_lander_template",
    "aio_flow",
    "aio_handle_key",
})
# An asset URL that names the tenant/landing in its path.
_ASSET_URL = re.compile(r"landing-cdn/", re.I)
MASK = "{hidden}"
_SHORT = 8


@dataclass(frozen=True)
class LeakHit:
    indicator_id: str
    capture_id: str
    kind: str
    hidden_kind: str
    hidden_value: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _pattern(value: str) -> re.Pattern[str]:
    esc = re.escape(value)
    if len(value) < _SHORT:
        return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", re.I)
    return re.compile(esc, re.I)


def hidden_values(ds: Dataset) -> dict[str, str]:
    """Hidden value -> the kind it came from."""
    out: dict[str, str] = {}
    for ind in ds.indicators():
        if ind.kind in HIDDEN_KINDS and ind.value:
            out.setdefault(ind.value, ind.kind)
    return out


def hide_platform_ids(ds: Dataset) -> tuple[Dataset, dict[str, str]]:
    """(dataset without the hidden layer, hidden value -> kind). The input is not modified."""
    values = hidden_values(ds)
    patterns = [(_pattern(v), v) for v in sorted(values, key=len, reverse=True)]
    out = ds.model_copy(deep=True)
    for inc in out.incidents:
        kept = []
        for ind in inc.indicators:
            if ind.kind in HIDDEN_KINDS:
                continue
            if ind.value is not None:
                if _ASSET_URL.search(ind.value):
                    continue
                masked = ind.value
                for pat, _ in patterns:
                    masked = pat.sub(MASK, masked)
                if masked != ind.value:
                    ind = ind.model_copy(update={"value": masked})
            kept.append(ind)
        inc.indicators = kept
    out.links, out.campaigns, out.assignments = [], [], []
    return Dataset.model_validate(out.model_dump()), values


def _variants(value: str) -> set[str]:
    """Spellings a hidden id can leak in: as is, without dashes, and a uuid's 8-hex prefix."""
    out = {value}
    if "-" in value:
        out.add(value.replace("-", ""))
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", value):
        out.add(value[:8])
    return out


def leak_check(hidden: Dataset, values: dict[str, str]) -> list[LeakHit]:
    """Every remaining indicator value that still contains a hidden value (expected: none).

    The search is wider than the masking in `hide_platform_ids`: it also looks for each id
    without dashes and for a uuid's 8-hex prefix, so it can catch what hiding missed.
    """
    patterns = [(_pattern(var), var, k) for v, k in values.items() for var in _variants(v)]
    hits = []
    for ind in hidden.indicators():
        if ind.value is None:
            continue
        for pat, v, k in patterns:
            if pat.search(ind.value):
                hits.append(LeakHit(ind.id, ind.capture_id, ind.kind, k, v))
    return hits
