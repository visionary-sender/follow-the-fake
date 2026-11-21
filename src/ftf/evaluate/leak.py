"""E1 hidden layer: hide the scammers' platform ids, then prove nothing of them is left.

`hide_platform_ids` does three things:

1. removes every indicator of a hidden kind (every platform config field: owner, campaign,
   tenant, landing, lander template, flow, handle key, source, form, advertiser, names);
2. drops indicators whose value is an asset URL with a hidden tenant or landing as a
   `landing-cdn/` path segment (content hashes stay; a landing-cdn URL of the platform's own
   account is a platform constant and stays);
3. masks every remaining occurrence of a hidden value as `{hidden}` in *every* string field of
   the dataset (indicator values, provenance locators and notes, artefact paths, source refs,
   ids), for example the handle-key parameter name inside a URL parameter shape. Masking is a
   pure function of the string, so references between ids stay consistent.

`leak_check` then searches every hidden value in every remaining string field,
case-insensitively, as is, without dashes and as a uuid's 8-hex prefix. Values of 8+ characters
match as substrings; shorter ones (a short numeric tenant or campaign id) only as whole tokens,
because a 3-digit substring hits random hashes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any

from ftf.contract import Dataset

# Indicator kinds that carry platform ids (the E1 ground truth). Nothing of these may stay visible.
# Platform constants (aio_sdk_asset, aio_action_files, platform_name) stay visible.
HIDDEN_KINDS = frozenset({
    "aio_owner",
    "aio_campaign",
    "aio_campaign_name",
    "aio_campaign_uuid",
    "aio_landing_name",
    "aio_source",
    "aio_form",
    "aio_advertiser",
    "aio_cdn_tenant",
    "aio_landing",
    "aio_lander_template",
    "aio_flow",
    "aio_handle_key",
})
# Kinds whose values are tenant or landing path segments of landing-cdn asset URLs.
_SEGMENT_KINDS = frozenset({"aio_cdn_tenant", "aio_landing"})
_ASSET_SEGMENTS = re.compile(r"landing-cdn/([^/?#\s\"'<>]+)(?:/([^/?#\s\"'<>]+))?", re.I)
MASK = "{hidden}"
_SHORT = 8


@dataclass(frozen=True)
class LeakHit:
    path: str  # JSON path of the field, e.g. incidents[3].indicators[17].value
    indicator_id: str | None  # the indicator the field belongs to, if any
    kind: str  # indicator kind, or the field name outside indicators
    hidden_kind: str
    hidden_value: str

    def as_dict(self) -> dict[str, Any]:
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


def _hidden_asset_url(value: str, segments: set[str]) -> bool:
    for m in _ASSET_SEGMENTS.finditer(value):
        if any(g and g.lower() in segments for g in m.groups()):
            return True
    return False


def _mask_all(node: Any, patterns: list[re.Pattern[str]]) -> Any:
    if isinstance(node, str):
        for pat in patterns:
            node = pat.sub(MASK, node)
        return node
    if isinstance(node, list):
        return [_mask_all(v, patterns) for v in node]
    if isinstance(node, dict):
        return {k: (v if k == "contract_version" else _mask_all(v, patterns)) for k, v in node.items()}
    return node


def hide_platform_ids(ds: Dataset) -> tuple[Dataset, dict[str, str]]:
    """(dataset without the hidden layer, hidden value -> kind). The input is not modified."""
    values = hidden_values(ds)
    segments = {v.lower() for v, k in values.items() if k in _SEGMENT_KINDS}
    out = ds.model_copy(deep=True)
    for inc in out.incidents:
        inc.indicators = [
            ind for ind in inc.indicators
            if ind.kind not in HIDDEN_KINDS and not (ind.value is not None and _hidden_asset_url(ind.value, segments))
        ]
    out.links, out.campaigns, out.assignments = [], [], []
    patterns = [_pattern(v) for v in sorted(values, key=len, reverse=True)]
    data = _mask_all(out.model_dump(mode="json"), patterns)
    return Dataset.model_validate(data), values


def _variants(value: str) -> set[str]:
    """Spellings a hidden id can leak in: as is, without dashes, and a uuid's 8-hex prefix."""
    out = {value}
    if "-" in value:
        out.add(value.replace("-", ""))
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", value):
        out.add(value[:8])
    return out


def _strings(node: Any, path: str = "", indicator: dict[str, Any] | None = None) -> Iterator[tuple[str, str, dict[str, Any] | None]]:
    """(json path, string, enclosing indicator dict) for every string field."""
    if isinstance(node, str):
        yield path, node, indicator
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]", indicator)
    elif isinstance(node, dict):
        is_indicator = path.rsplit(".", 1)[-1].startswith("indicators[")
        for k, v in node.items():
            if k == "contract_version":
                continue
            yield from _strings(v, f"{path}.{k}" if path else k, node if is_indicator else indicator)


def leak_check(hidden: Dataset, values: dict[str, str]) -> list[LeakHit]:
    """Every remaining string field that still contains a hidden value (expected: none).

    The search is wider than the masking in `hide_platform_ids`: it also looks for each id
    without dashes and for a uuid's 8-hex prefix, so it can catch what hiding missed.
    """
    patterns = [(_pattern(var), v, k) for v, k in values.items() for var in _variants(v)]
    hits = []
    for path, text, ind in _strings(hidden.model_dump(mode="json")):
        for pat, v, k in patterns:
            if pat.search(text):
                kind = ind["kind"] if ind else path.rsplit(".", 1)[-1]
                hits.append(LeakHit(path, ind["id"] if ind else None, kind, k, v))
    return hits
