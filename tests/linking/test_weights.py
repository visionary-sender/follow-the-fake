"""HAND_TABLE entries for Ingestion's kinds, on synthetic captures with placeholder values."""

from __future__ import annotations

import pytest

from ftf.contract.models import (
    COLLECTOR_KINDS,
    Capture,
    Dataset,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Relation,
)
from ftf.linking import HAND_TABLE, ScoringContext, score_pair
from ftf.linking.ledger import has_private_conflict

L = Layer


def ind(layer: Layer, kind: str, value: str, live: bool = False) -> tuple:
    return (layer, kind, value, live)


def incident(iid: str, inds: list[tuple]) -> Incident:
    cid = f"{iid}/lander"
    return Incident(
        id=iid,
        source="synthetic",
        captures=[Capture(id=cid, stage="lander")],
        indicators=[
            Indicator(
                id=f"{cid}/{kind}/{n}", capture_id=cid, layer=layer, kind=kind, level=Level.campaign,
                value=value, observed=Observed.live if live else Observed.static, extractor="synthetic@0",
            )
            for n, (layer, kind, value, live) in enumerate(inds)
        ],
    )


def row(score, kind):
    (r,) = [r for r in score.rows if r.kind == kind]
    return r

# Linkable kinds Ingestion emits (kind names only, no values). Copied from `ftf.indicators._ids.KINDS`
# on the branch; switch to importing it once that package is on main.
INGESTION_KINDS = {
    "aio_action_files", "aio_campaign", "aio_campaign_name", "aio_cdn_tenant", "aio_flow", "aio_form",
    "aio_handle_key", "aio_lander_template", "aio_landing", "aio_landing_name", "aio_owner", "aio_sdk_asset",
    "aio_source", "capi_gateway", "clarity_id", "contact_email", "contact_phone", "form_endpoint",
    "form_field_set", "google_ads_id", "google_analytics_id", "meta_ad_id", "meta_ad_name", "meta_adset_id",
    "meta_adset_name", "meta_campaign_id", "meta_campaign_name", "meta_pixel", "platform_name",
    "push_client_id", "tracker_postback", "video_cdn_tenant", "xid_tracker_machine",
    "aio_advertiser", "contact_telegram", "gtm_container", "offer_id", "tiktok_pixel", "yandex_metrica_id",
    # kit and page-structure fingerprints
    "asset_sha256", "asset_norm_sha256", "inline_script_sha256", "dom_skeleton_sha256", "dom_skeleton_lsh",
    "page_url_shape", "form_action_shape", "third_party_host", "third_party_domain",
}


def test_every_ingestion_kind_has_an_explicit_entry():
    assert not INGESTION_KINDS - set(HAND_TABLE.kinds)
    assert not set(HAND_TABLE.kinds) & COLLECTOR_KINDS


def test_entries_are_sane():
    for kind, w in HAND_TABLE.kinds.items():
        assert 0.0 <= w.bits <= 7.0, kind
        if w.relations is not None:
            assert set(w.relations) == set(Relation), kind
            assert all(0.0 <= s <= 1.0 for s in w.relations.values()), kind
    # single-valued ids are exactly the per-page platform, tenant, owner and aio campaign
    singles = {k for k, w in HAND_TABLE.kinds.items() if w.single_valued}
    assert singles == {"platform_name", "aio_cdn_tenant", "aio_owner", "aio_campaign"}


def _pair(a: list[tuple], b: list[tuple]) -> ScoringContext:
    filler = [incident(f"f{i}", [ind(L.platform, "platform_name", "stack-f")]) for i in range(3)]
    return ScoringContext.from_dataset(Dataset(name="pair", incidents=[incident("x", a), incident("y", b)] + filler))


def test_aio_owner_conflict_is_cannot_link():
    common = [
        ind(L.tenant, "aio_cdn_tenant", "tenant-1", live=True),
        ind(L.landing, "aio_landing", "landing-1"),
        ind(L.tracking_account, "meta_pixel", "pixel-1", live=True),
    ]
    ctx = _pair(common + [ind(L.owner, "aio_owner", "owner-1")], common + [ind(L.owner, "aio_owner", "owner-2")])
    s = score_pair(ctx, "x/lander", "y/lander", Relation.campaign)
    assert row(s, "aio_owner").status is EvidenceStatus.conflict
    assert row(s, "aio_owner").bits == HAND_TABLE.conflict_bits


def test_differing_aio_campaign_under_one_owner_is_a_campaign_conflict_but_no_cannot_link():
    owner = [ind(L.owner, "aio_owner", "owner-1"), ind(L.tenant, "aio_cdn_tenant", "tenant-1")]
    ctx = _pair(owner + [ind(L.owner, "aio_campaign", "camp-1")], owner + [ind(L.owner, "aio_campaign", "camp-2")])
    s = score_pair(ctx, "x/lander", "y/lander", Relation.campaign)
    assert [r.kind for r in s.rows if r.status is EvidenceStatus.conflict] == ["aio_campaign"]
    assert not has_private_conflict(s)  # same owner: a different campaign, never a cannot-link
    op = score_pair(ctx, "x/lander", "y/lander", Relation.operator)
    assert not [r for r in op.rows if r.status is EvidenceStatus.conflict]
    # under campaign the shared owner is explained away by the differing campaign under it
    assert row(s, "aio_owner").status is EvidenceStatus.hub_subsumed and "differing aio campaign" in row(s, "aio_owner").note


@pytest.mark.parametrize("kind", ["meta_pixel", "capi_gateway", "xid_tracker_machine"])
def test_multi_valued_tracker_shares_one_value_without_conflict(kind):
    layer = HAND_TABLE.kinds[kind].layer
    a = [ind(layer, kind, "trk-1", live=True), ind(layer, kind, "trk-2", live=True)]
    b = [ind(layer, kind, "trk-1", live=True), ind(layer, kind, "trk-3", live=True)]
    s = score_pair(_pair(a, b), "x/lander", "y/lander", Relation.operator)  # campaign: gated
    assert row(s, kind).status is EvidenceStatus.shared
    assert row(s, kind).bits == pytest.approx(3.0)
    assert s.live_private
    disjoint = score_pair(_pair(a, [ind(layer, kind, "trk-9", live=True)]), "x/lander", "y/lander")
    assert not disjoint.rows  # a differing multi-valued id is no evidence either way


def test_template_kinds_count_for_template_weakly_for_campaign():
    tpl = [ind(L.kit, "aio_lander_template", "tpl-1"), ind(L.tenant, "aio_flow", "flow-1")]
    ctx = _pair(tpl, tpl)
    t = score_pair(ctx, "x/lander", "y/lander", Relation.template)
    c = score_pair(ctx, "x/lander", "y/lander", Relation.campaign)
    assert row(t, "aio_lander_template").bits == pytest.approx(3.0)  # 4 − log2(2 groups)
    assert row(t, "aio_flow").bits == pytest.approx(2.0)  # 3 − log2(2 groups)
    assert c.bits <= 2.0 < t.bits


def test_analytics_ids_are_not_private():
    """A copied outlet article fires the outlet's analytics: a shared one alone gives no strict link."""
    a = [ind(L.tracking_account, "google_analytics_id", "ga-1", live=True)]
    s = score_pair(_pair(a, a), "x/lander", "y/lander", Relation.campaign)
    assert row(s, "google_analytics_id").status is EvidenceStatus.shared
    assert not s.live_private


@pytest.mark.parametrize(
    "kind", ["asset_sha256", "asset_norm_sha256", "inline_script_sha256", "dom_skeleton_sha256", "dom_skeleton_lsh",
             "page_url_shape", "form_action_shape", "form_endpoint", "form_field_set"],
)
def test_template_hints_clear_hub_bits_for_a_pair(kind):
    layer = HAND_TABLE.kinds[kind].layer
    v = [ind(layer, kind, "fp-1")]
    s = score_pair(_pair(v, v), "x/lander", "y/lander", Relation.template)
    assert row(s, kind).bits >= HAND_TABLE.hub_bits
