"""Stopgap adapter: planning-stage prototype signals -> contract `Dataset`.

Reads `signals.json` (per-node signal values) and the case cards `cases-backup.json` /
`cases-shared.json` (analyst-extracted platform ids from `window.aio`) produced during
planning. It is replaced by Ingestion's contract output (HACK-9/HACK-10): evaluation code only
ever sees a `Dataset`, so switching is a path change (`--dataset`), not a rewrite.

The whole kind -> layer/level table is `KIND_TABLE` (signal subfamilies) plus `CARD_TABLE`
(case-card platform ids). Collector kinds go to the collector layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ftf.contract import (
    COLLECTOR_KINDS,
    Capture,
    Dataset,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
)
from ftf.evaluate.units import duplicate_of, node_incident, node_source_key, node_stage, node_to_capture

EXTRACTOR = "prototype-signals@0.1"
DATASET_NAME = "prototype-contract-v0"

L, V = Layer, Level
# signals.json subfamily -> (contract kind, layer, level). Kinds already used by the v0 fixture keep
# its names (meta_pixel, aio_cdn_tenant, ...). Values from recorded traffic families are `live`.
KIND_TABLE: dict[str, tuple[str, Layer, Level]] = {
    # 1 files
    "content_sha256_kit": ("content_sha256_kit", L.kit, V.platform),
    "content_sha256_generic": ("content_sha256_generic", L.infrastructure, V.ecosystem),
    "resource_basename": ("resource_basename", L.kit, V.platform),
    # 2 hosts
    "host_thirdparty": ("host_thirdparty", L.infrastructure, V.operator),
    "regdomain_thirdparty": ("regdomain_thirdparty", L.infrastructure, V.operator),
    "host_benign": ("host_benign", L.infrastructure, V.ecosystem),
    "tenant_domain": ("tenant_domain", L.infrastructure, V.ecosystem),
    # 3 infrastructure
    "server_ip": ("server_ip", L.infrastructure, V.ecosystem),
    "http_header": ("http_header", L.infrastructure, V.platform),
    "x_header_name": ("x_header_name", L.platform, V.platform),
    "cookie_name": ("cookie_name", L.platform, V.platform),
    "header_order_doc": ("header_order_doc", L.infrastructure, V.platform),
    "tls": ("tls", L.infrastructure, V.platform),
    "redirect": ("redirect", L.infrastructure, V.operator),
    "path_template": ("path_template", L.kit, V.platform),
    "param_set": ("param_set", L.tracker_instance, V.platform),
    "param_shape": ("param_shape", L.tracker_instance, V.platform),
    "js_redirect": ("js_redirect", L.tracker_instance, V.operator),
    "api_endpoint": ("api_endpoint", L.platform, V.platform),
    "xid_machine": ("xid_machine", L.tracker_instance, V.operator),
    "capi_gateway": ("capi_gateway", L.tracking_account, V.operator),
    # 4 ids
    "fb_pixel": ("meta_pixel", L.tracking_account, V.operator),
    "ga_id": ("google_analytics_id", L.tracking_account, V.operator),
    "gtm_id": ("google_tag_manager_id", L.tracking_account, V.operator),
    "gads_id": ("google_ads_id", L.tracking_account, V.operator),
    "clarity_id": ("clarity_id", L.tracking_account, V.operator),
    "tracking_param_value": ("tracking_param_value", L.distribution_account, V.campaign),
    "fb_ad_id": ("meta_ad_id", L.distribution_account, V.campaign),
    "form_action": ("form_action", L.kit, V.platform),
    "hidden_field": ("hidden_field", L.offer, V.campaign),
    "form_fieldset": ("form_fieldset", L.kit, V.platform),
    "email": ("email", L.offer, V.operator),
    "phone": ("phone", L.offer, V.operator),
    "google_api_key": ("google_api_key", L.tracking_account, V.operator),
    "push_client_id": ("push_client_id", L.tracking_account, V.operator),
    "video_cdn_id": ("video_cdn_id", L.tenant, V.tenant),
    "landing_cdn_account": ("aio_cdn_tenant", L.tenant, V.tenant),
    "landing_cdn_landing": ("aio_landing", L.landing, V.landing),
    "action_files_id": ("aio_action_files", L.platform, V.platform),
    "lander_path": ("lander_path", L.kit, V.campaign),
    "keitaro_like": ("keitaro_like", L.tracker_instance, V.platform),
    "adlib_playlist_uploader_id": ("adlib_playlist_uploader_id", L.distribution_account, V.operator),
    # 5 html
    "css_class": ("css_class", L.kit, V.platform),
    "dom_id": ("dom_id", L.kit, V.platform),
    "html_comment": ("html_comment", L.kit, V.campaign),
    "meta": ("html_meta", L.kit, V.platform),
    "html_lang": ("html_lang", L.kit, V.platform),
    "html_style": ("html_style", L.kit, V.platform),
    "inline_script_hash": ("inline_script_hash", L.kit, V.platform),
    "js_norm_hash": ("js_norm_hash", L.kit, V.platform),
    "base_href_shape": ("base_href_shape", L.kit, V.platform),
    "script_src_base": ("script_src_base", L.kit, V.platform),
    "link_href_base": ("link_href_base", L.kit, V.platform),
    "title": ("page_title", L.creative, V.campaign),
    "css_selector_class": ("css_selector_class", L.kit, V.platform),
    "font_family": ("font_family", L.kit, V.platform),
    "pwa_name": ("pwa_name", L.creative, V.campaign),
    "aio_sdk_version": ("aio_sdk_asset", L.platform, V.platform),
    "macros_file": ("aio_macros_file", L.kit, V.tenant),
    "capi_plugin": ("capi_plugin", L.tracker_instance, V.platform),
    "input_name": ("input_name", L.kit, V.platform),
    "js_string_lit": ("js_string_lit", L.kit, V.platform),
    "hue_rotate": ("uniqueizer_hue_rotate", L.platform, V.platform),
    # 6 text, 7 images
    "text_ngram8": ("text_ngram8", L.creative, V.campaign),
    "name_bigram": ("name_bigram", L.creative, V.campaign),
    "image_sha_kit": ("image_sha_kit", L.creative, V.campaign),
    "image_phash_cluster": ("image_phash_cluster", L.creative, V.campaign),
    # 8 time and per-visit values: describe the server or the visit
    "last_modified": ("last_modified", L.infrastructure, V.landing),
    "cert_not_before": ("cert_not_before", L.infrastructure, V.landing),
    # collector: the capture or the analyst's visit, never the scam
    "capture_day": ("capture_day", L.collector, V.capture),
    "capture_time": ("capture_time", L.collector, V.capture),
    "screenshot_date": ("screenshot_date", L.collector, V.capture),
    "saved_from": ("local_path", L.collector, V.capture),
    "uuid7_day": ("uuid7_day", L.collector, V.capture),
    "uuid7_in_url": ("uuid7_in_url", L.collector, V.capture),
    "xid_day": ("xid_day", L.collector, V.capture),
    "xid_clickid": ("xid_clickid", L.collector, V.capture),
}
# Families seen in recorded traffic (HAR): observed = live.
LIVE_SUBFAMILIES = {
    "server_ip", "http_header", "x_header_name", "cookie_name", "header_order_doc", "tls", "redirect",
    "api_endpoint", "capi_gateway", "xid_machine",
}
UNMAPPED = ("unmapped_signal", L.kit, V.platform)  # fallback for a subfamily not in the table

# Case-card platform ids (window.aio config) -> (kind, layer, level). These are the E1 hidden layer.
CARD_TABLE: dict[str, tuple[str, Layer, Level]] = {
    "aio_owner": ("aio_owner", L.owner, V.operator),
    "aio_campaign": ("aio_campaign", L.owner, V.campaign),
    "aio_campaign_uuid": ("aio_campaign_uuid", L.owner, V.campaign),
    "aio_cdn_tenant": ("aio_cdn_tenant", L.tenant, V.tenant),
    "aio_landing": ("aio_landing", L.landing, V.landing),
    "aio_lander_template": ("aio_lander_template", L.kit, V.platform),
    "aio_flow": ("aio_flow", L.tracker_instance, V.operator),
    "aio_handle_key": ("aio_handle_key", L.tenant, V.tenant),
}
# Card field spellings (BACKUP dict keys / do_udostepnienia "key:value" prefixes) -> CARD_TABLE key.
_CARD_ALIASES = {
    "aio_owner_uuid": "aio_owner", "aio_owner": "aio_owner",
    "aio_tenant_path": "aio_cdn_tenant", "aio_cdn_tenant": "aio_cdn_tenant",
    "aio_template_uuid": "aio_lander_template", "aio_lander_template": "aio_lander_template",
    "aio_flow_uuid": "aio_flow", "aio_flow": "aio_flow",
    "aio_handle_key": "aio_handle_key", "aio_landing": "aio_landing",
    "aio_campaign": "aio_campaign", "aio_campaign_human_id": "aio_campaign",
}
for _k, (_kind, _layer, _) in KIND_TABLE.items():
    assert _kind not in COLLECTOR_KINDS or _layer is Layer.collector, _k

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UUID_PREFIX = re.compile(r"\b([0-9a-f]{8})-\.\.\.")
_HUMAN_ID = re.compile(r"human id (\d+)")
_TOKENS = [
    (re.compile(r"(access_token=)[^&\s\"'<>]+", re.I), r"\1<redacted>"),
    (re.compile(r"\bEAA[A-Za-z0-9]{12,}"), "<redacted-meta-token>"),
]


def scrub(value: str) -> str:
    """Remove Meta access tokens (never copied into outputs or logs)."""
    for pat, rep in _TOKENS:
        value = pat.sub(rep, value)
    return value


def _card_ids(card: dict[str, Any]) -> dict[str, list[str]]:
    """Platform ids of one case card, normalised: campaign key = human id, else campaign uuid."""
    raw = card.get("notable_ids") or {}
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(k, str(v)) for k, v in raw.items()]
    else:
        for item in raw:
            k, sep, v = str(item).partition(":")
            if sep:
                pairs.append((k.strip(), v.strip()))
    out: dict[str, list[str]] = {}

    def put(key: str, value: str) -> None:
        if value and value not in out.setdefault(key, []):
            out[key].append(value)

    for k, v in pairs:
        key = _CARD_ALIASES.get(k)
        if key is None:
            continue
        if key == "aio_campaign":
            human = _HUMAN_ID.search(v)
            uuid = _UUID.search(v) or _UUID_PREFIX.search(v)
            if k == "aio_campaign_human_id" or v.isdigit():
                put("aio_campaign_human", v)
            elif human:
                put("aio_campaign_human", human.group(1))
            if uuid:
                put("aio_campaign_uuid", uuid.group(1) if uuid.re is _UUID_PREFIX else uuid.group(0))
            continue
        if key in ("aio_landing",):
            m = _UUID.search(v)
            if m:
                put(key, m.group(0))
            continue
        m = _UUID.search(v)
        put(key, m.group(0) if m else v.split()[0])
    # campaign key: human id when the card has one, else the uuid
    out["aio_campaign"] = out.pop("aio_campaign_human", None) or list(out.get("aio_campaign_uuid", []))
    if not out["aio_campaign"]:
        out.pop("aio_campaign")
    return out


def _cards(proto_dir: Path) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    shared = proto_dir / "cases-shared.json"
    backup = proto_dir / "cases-backup.json"
    if shared.is_file():
        for c in json.loads(shared.read_text(encoding="utf-8"))["cases"]:
            cards[str(c["case_id"])] = c
    if backup.is_file():
        for c in json.loads(backup.read_text(encoding="utf-8")):
            cards[str(c["case_id"])] = c
    return cards


def _capture_date(card: dict[str, Any] | None) -> str | None:
    if not card:
        return None
    day = card.get("capture_date") or (card.get("artifacts") or {}).get("capture_date")
    return day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None


def build_dataset(proto_dir: str | Path) -> Dataset:
    """Convert the prototype files in `proto_dir` into a validated contract Dataset."""
    proto_dir = Path(proto_dir)
    signals = json.loads((proto_dir / "signals.json").read_text(encoding="utf-8"))
    cards = _cards(proto_dir)
    nodes = [n for n in signals if not n.startswith("_")]

    by_incident: dict[str, dict[str, Any]] = {}
    for node in nodes:
        key = node_source_key(node)
        inc = by_incident.setdefault(
            node_incident(node),
            {"source": key.source, "folder": key.folder, "captures": [], "indicators": []},
        )
        cap_id = node_to_capture(node)
        inc["captures"].append(
            Capture(
                id=cap_id,
                stage=node_stage(node),
                captured_on=_capture_date(cards.get(key.folder)),
                duplicate_of=duplicate_of(node),
            )
        )
        seen: set[tuple[str, str]] = set()

        def add(kind: str, layer: Layer, level: Level, value: str, observed: Observed, note: str) -> None:
            value = scrub(value)
            if (kind, value) in seen:
                return
            seen.add((kind, value))
            inc["indicators"].append(
                Indicator(
                    id=f"{cap_id}/{kind}/{sum(1 for k, _ in seen if k == kind) - 1}",
                    capture_id=cap_id, layer=layer, kind=kind, level=level, value=value,
                    observed=observed, provenance=Provenance(note=note), extractor=EXTRACTOR,
                )
            )

        # The platform's own action-files account also shows up as a landing-cdn path segment; it
        # names the platform, not a tenant, so it is emitted once as aio_action_files only.
        platform_accounts = set(signals[node].get("action_files_id") or [])
        for sub, values in signals[node].items():
            kind, layer, level = KIND_TABLE.get(sub, UNMAPPED)
            observed = Observed.live if sub in LIVE_SUBFAMILIES else Observed.static
            for v in values if isinstance(values, list) else [values]:
                text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
                if not text or (sub == "landing_cdn_account" and text in platform_accounts):
                    continue
                add(kind, layer, level, text, observed, f"prototype signals.json {sub}")

        # Case-card platform ids belong to the captures that show the platform (an aio tenant path).
        # A card describes the whole case, so in a multi-capture case its landing ids are not
        # attributable to one capture and are left to the per-capture signals.
        card = cards.get(key.folder)
        multi = sum(1 for n in nodes if n.split("/", 1)[0] == key.folder) > 1
        if card and "landing_cdn_account" in signals[node]:
            for ck, values in _card_ids(card).items():
                if ck not in CARD_TABLE or (multi and ck == "aio_landing"):
                    continue
                kind, layer, level = CARD_TABLE[ck]
                for v in values:
                    add(kind, layer, level, v, Observed.static, f"case card notable_ids ({ck})")

    incidents = [
        Incident(id=iid, source=d["source"], source_ref=d["folder"], captures=d["captures"], indicators=d["indicators"])
        for iid, d in sorted(by_incident.items())
    ]
    return Dataset(
        name=DATASET_NAME,
        description="Planning-stage prototype signals converted to contract 0.1 (stopgap until Ingestion, HACK-9/10).",
        incidents=incidents,
    )


def kind_table_markdown() -> str:
    """The full mapping as a Markdown table (for the HACK-26 comment and docs)."""
    rows = ["| source field | kind | layer | level | observed |", "|---|---|---|---|---|"]
    for sub, (kind, layer, level) in KIND_TABLE.items():
        obs = "live" if sub in LIVE_SUBFAMILIES else "static"
        rows.append(f"| signals `{sub}` | `{kind}` | {layer.value} | {level.value} | {obs} |")
    for ck, (kind, layer, level) in CARD_TABLE.items():
        rows.append(f"| case card `{ck}` | `{kind}` | {layer.value} | {level.value} | static |")
    return "\n".join(rows)
