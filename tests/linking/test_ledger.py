"""Tiered evidence ledger on synthetic captures (placeholder values only, no case data).

The three decisions of 07 §2.5 are rebuilt with the same structure:
(a) new capture on a new domain vs a known campaign: platform + tenant + landing + live pixel + caption;
(b) a page on another platform sharing only a creative pack and a brand skin;
(c) a copied kit on another stack: same template, campaign unresolved.
"""

from __future__ import annotations

import pytest

from ftf.cli import main
from ftf.contract.models import (
    Capture,
    Dataset,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Level,
    LinkLevel,
    Observed,
    Relation,
)
from ftf.linking import HAND_TABLE, ScoringContext, score_pair, threshold, to_link
from ftf.linking.ledger import verdict

L = Layer


def ind(layer: Layer, kind: str, value: str, live: bool = False) -> tuple:
    return (layer, kind, value, live)


def incident(iid: str, inds: list[tuple], stage: str = "lander", cap: str = "lander", dup: str | None = None) -> Incident:
    cid = f"{iid}/{cap}"
    return Incident(
        id=iid,
        source="synthetic",
        captures=[Capture(id=cid, stage=stage, duplicate_of=dup)],
        indicators=[
            Indicator(
                id=f"{cid}/{kind}/{n}", capture_id=cid, layer=layer, kind=kind, level=Level.campaign,
                value=value, observed=Observed.live if live else Observed.static, extractor="synthetic@0",
            )
            for n, (layer, kind, value, live) in enumerate(inds)
        ],
    )


AIO = [ind(L.platform, "platform_name", "stack-a"), ind(L.platform, "aio_sdk_asset", "sdk-const-1")]
CAMPAIGN_A = AIO + [
    ind(L.tenant, "aio_cdn_tenant", "tenant-a"),
    ind(L.landing, "aio_landing", "landing-a"),
    ind(L.tracking_account, "meta_pixel", "pixel-a", live=True),
    ind(L.creative, "ad_caption", "Placeholder caption A"),
    ind(L.distribution_account, "advertiser_page_name", "page-a1"),
]


def base_incidents() -> list[Incident]:
    return [
        incident("a1", CAMPAIGN_A),
        incident("a2", CAMPAIGN_A[:-1] + [ind(L.distribution_account, "advertiser_page_name", "page-a2")]),
        # (a) new domain, new page; everything else as campaign A (caption with other spacing/case)
        incident("n", CAMPAIGN_A[:-2] + [
            ind(L.creative, "ad_caption", "placeholder  CAPTION a"),
            ind(L.distribution_account, "advertiser_page_name", "page-new"),
            ind(L.infrastructure, "a_record_ip", "192.0.2.10"),
        ]),
        # cluster B: same platform, own tenant, a creative pack and brand skin
        incident("b1", AIO + [
            ind(L.tenant, "aio_cdn_tenant", "tenant-b"),
            ind(L.creative, "image_phash", "pack-1"),
            ind(L.kit, "brand_asset", "brand-logo"),
        ]),
        incident("b2", AIO + [ind(L.tenant, "aio_cdn_tenant", "tenant-b"), ind(L.kit, "brand_asset", "brand-logo")]),
        # (b) distractor on another platform
        incident("d", [
            ind(L.platform, "platform_name", "stack-b"),
            ind(L.creative, "image_phash", "pack-1"),
            ind(L.kit, "brand_asset", "brand-logo"),
        ]),
        # (c) copied kit: same asset set, title and static (copied) pixel, on another platform
        incident("k1", [
            ind(L.platform, "platform_name", "stack-c"),
            ind(L.kit, "kit_asset_set", "kitset-x"),
            ind(L.creative, "page_title", "Placeholder title X"),
            ind(L.tracking_account, "meta_pixel", "pixel-x"),
        ]),
        incident("k2", AIO + [
            ind(L.kit, "kit_asset_set", "kitset-x"),
            ind(L.creative, "page_title", "Placeholder title X"),
            ind(L.tracking_account, "meta_pixel", "pixel-x"),
        ]),
    ] + [
        incident(f"f{i}", AIO + [ind(L.tenant, "aio_cdn_tenant", f"tenant-f{i}"), ind(L.kit, "brand_asset", "brand-logo")])
        for i in range(4)
    ]


@pytest.fixture
def ds() -> Dataset:
    return Dataset(name="synthetic", incidents=base_incidents())


@pytest.fixture
def ctx(ds: Dataset) -> ScoringContext:
    return ScoringContext.from_dataset(ds)


def row(score, kind):
    (r,) = [r for r in score.rows if r.kind == kind]
    return r


def test_rows_sum_to_total(ctx):
    for a, b in [("n/lander", "a1/lander"), ("d/lander", "b1/lander"), ("k1/lander", "k2/lander")]:
        for rel in Relation:
            s = score_pair(ctx, a, b, rel)
            assert sum(r.bits for r in s.rows) == pytest.approx(s.bits, abs=1e-6)


def test_a_new_domain_assigned_to_known_campaign(ctx):
    s = score_pair(ctx, "n/lander", "a1/lander", Relation.campaign)
    tau = threshold(ctx, Relation.campaign)
    assert s.bits >= tau + 2
    assert verdict(s, ctx) == "link"
    assert s.live_private and s.layers_positive >= 3
    assert row(s, "aio_cdn_tenant").status is EvidenceStatus.shared
    assert row(s, "aio_cdn_tenant").bits > row(s, "aio_landing").bits > 0
    # the three campaign-A incidents share a live pixel: one pre-group, so no rarity penalty
    assert row(s, "aio_landing").seen_in == "1 group (3 incidents) / 10"
    # the landing id is conditioned on the shared tenant (chain rule, HACK-23), never above its hand weight
    assert 0 < row(s, "aio_landing").bits <= HAND_TABLE.kinds["aio_landing"].bits
    assert "given the shared tenant" in row(s, "aio_landing").note
    # platform constants are everywhere: one hub row per kind, no bits
    assert row(s, "aio_sdk_asset").status is EvidenceStatus.hub_subsumed
    assert row(s, "aio_sdk_asset").bits == 0
    # caption matches after normalisation
    assert row(s, "ad_caption").status in (EvidenceStatus.shared, EvidenceStatus.hub_subsumed)
    # rotating page: no shared value, not single-valued, so no row and no penalty
    assert not [r for r in s.rows if r.kind == "advertiser_page_name"]
    assert all(r.bits >= 0 for r in s.rows)


def test_b_distractor_on_other_platform_is_new(ctx):
    s = score_pair(ctx, "d/lander", "b1/lander", Relation.campaign)
    tau = threshold(ctx, Relation.campaign)
    assert s.bits <= tau - 3
    assert verdict(s, ctx) == "no link"
    assert row(s, "platform_name").status is EvidenceStatus.conflict
    assert row(s, "platform_name").bits == HAND_TABLE.conflict_bits
    assert row(s, "image_phash").bits > 0
    assert row(s, "brand_asset").status is EvidenceStatus.hub_subsumed
    # distractor has no tenant layer: a missing row, 0 bits
    assert row(s, "aio_cdn_tenant").status is EvidenceStatus.missing
    assert row(s, "aio_cdn_tenant").bits == 0
    # the ecosystem relation does not penalise a platform difference and still sees the creative pack
    eco = score_pair(ctx, "d/lander", "b1/lander", Relation.ecosystem)
    assert not [r for r in eco.rows if r.status is EvidenceStatus.conflict]
    assert eco.bits > 0


def test_c_copied_kit_same_template_campaign_in_review(ctx):
    tmpl = score_pair(ctx, "k1/lander", "k2/lander", Relation.template)
    camp = score_pair(ctx, "k1/lander", "k2/lander", Relation.campaign)
    tau = threshold(ctx, Relation.campaign)
    assert tmpl.bits > 0
    assert row(tmpl, "kit_asset_set").bits == pytest.approx(5.5)  # 07 §2.5 c: template +5.5
    # clears τ for template, but on one layer only: the D1 gate keeps it in review
    assert tmpl.bits >= threshold(ctx, Relation.template)
    assert verdict(tmpl, ctx) == "review"
    assert row(camp, "platform_name").status is EvidenceStatus.conflict
    assert row(tmpl, "meta_pixel").bits == 0  # a pixel says nothing about the template
    assert tau - 3 <= camp.bits < tau
    assert verdict(camp, ctx) == "review"
    # pixel copied into saved HTML: static, so no strict link and a copy-prior discount
    assert not camp.live_private
    assert row(camp, "meta_pixel").bits < row(
        score_pair(ctx, "n/lander", "a1/lander", Relation.campaign), "meta_pixel"
    ).bits + HAND_TABLE.static_discount


def test_big_campaign_ids_not_punished():
    """07 §2.2 item 2: a campaign of six incidents keeps the full weight of its own ids."""
    members = [incident(f"a{i}", CAMPAIGN_A) for i in range(6)]
    others = [
        incident(f"o{i}", AIO + [ind(L.tenant, "aio_cdn_tenant", f"tenant-o{i}"),
                                 ind(L.tracking_account, "meta_pixel", f"pixel-o{i}", live=True)])
        for i in range(20)
    ]
    new = incident("n", CAMPAIGN_A[:-2] + [ind(L.creative, "ad_caption", "Placeholder caption A")])
    ctx = ScoringContext.from_dataset(Dataset(name="big-campaign", incidents=members + others + [new]))
    assert ctx.n_groups == 21
    s = score_pair(ctx, "n/lander", "a0/lander")
    assert row(s, "aio_cdn_tenant").bits == pytest.approx(5.0)  # 6 − 1 static copy prior, k = 1 group
    assert "log2(1) rarity" in row(s, "aio_landing").note and row(s, "aio_landing").bits > 0  # no rarity loss
    assert row(s, "meta_pixel").bits == pytest.approx(3.0)
    assert row(score_pair(ctx, "n/lander", "a0/lander", Relation.operator), "meta_pixel").bits == pytest.approx(3.0)
    assert s.bits >= threshold(ctx, Relation.campaign) + 3
    assert verdict(s, ctx) == "link"


def _owner_campaigns() -> Dataset:
    """One owner, two of its campaigns, platform ids hidden (E1): only the owner's live CAPI gateway, its kit
    asset, a foreign host and the platform SDK are shared; plus unrelated fillers so the threshold is realistic."""
    owner = [
        ind(L.tracking_account, "capi_gateway", "gateway-1", live=True),
        ind(L.kit, "asset_norm_sha256", "asset-1", live=True),
        ind(L.infrastructure, "third_party_host", "host-1", live=True),
        ind(L.platform, "aio_sdk_asset", "sdk-1", live=True),
    ]
    fillers = [incident(f"f{i}", [ind(L.landing, "aio_landing", f"landing-f{i}")]) for i in range(10)]
    return Dataset(name="owner-campaigns", incidents=[
        incident("c1", owner + [ind(L.landing, "aio_landing", "landing-1"), ind(L.creative, "ad_caption", "Caption one")]),
        incident("c2", owner + [ind(L.landing, "aio_landing", "landing-2"), ind(L.creative, "ad_caption", "Caption two")]),
        # a second capture of campaign 1: same live pixel and gateway, same landing
        incident("c1b", owner + [ind(L.landing, "aio_landing", "landing-1")]),
    ] + fillers)


def test_owner_accounts_and_kit_do_not_make_a_campaign():
    """HACK-53: two campaigns of one owner sharing only the CAPI gateway and kit: operator link, never a campaign link."""
    ctx = ScoringContext.from_dataset(_owner_campaigns())
    camp = score_pair(ctx, "c1/lander", "c2/lander", Relation.campaign)
    op = score_pair(ctx, "c1/lander", "c2/lander", Relation.operator)
    assert row(camp, "capi_gateway").bits == pytest.approx(3.0)  # full bits: the gate, not the scale, keeps it apart
    assert row(op, "capi_gateway").bits == pytest.approx(3.0)
    assert not camp.live_private and camp.layers_positive == 1  # campaign gate: no campaign-level row
    assert verdict(camp, ctx) != "link"
    assert to_link(camp, "L", ctx).level is LinkLevel.loose
    assert op.live_private
    assert verdict(op, ctx) == "link"
    # even with ample bits, operator-level layers alone stay in review under campaign
    from ftf.linking.types import PairScore
    rich = PairScore(**{**camp.__dict__, "bits": 50.0})
    assert verdict(rich, ctx) == "review"


def test_same_campaign_pixel_and_landing_links():
    """HACK-53: a shared live CAPI gateway plus the same landing is a campaign link."""
    ctx = ScoringContext.from_dataset(_owner_campaigns())
    s = score_pair(ctx, "c1/lander", "c1b/lander", Relation.campaign)
    assert row(s, "aio_landing").bits > 0
    assert s.live_private and s.layers_positive >= 2
    assert verdict(s, ctx) == "link"


def test_operator_level_kinds_in_campaign_layers_do_not_open_the_gate():
    """HACK-53 review: an advertiser page or a cloned page title sits in a campaign-ish layer but is shared
    by all campaigns of the account or template; with the owner's live gateway it is still no campaign link."""
    shared = [
        ind(L.tracking_account, "capi_gateway", "gateway-1", live=True),
        ind(L.distribution_account, "advertiser_page_name", "Page one"),
        ind(L.creative, "page_title", "Title one"),
        ind(L.kit, "asset_norm_sha256", "asset-1", live=True),
    ]
    fillers = [incident(f"f{i}", [ind(L.landing, "aio_landing", f"landing-f{i}")]) for i in range(10)]
    ds = Dataset(name="page", incidents=[
        incident("c1", shared + [ind(L.landing, "aio_landing", "landing-1")]),
        incident("c2", shared + [ind(L.landing, "aio_landing", "landing-2")]),
    ] + fillers)
    ctx = ScoringContext.from_dataset(ds)
    s = score_pair(ctx, "c1/lander", "c2/lander", Relation.campaign)
    assert row(s, "advertiser_page_name").bits > 0
    assert not s.live_private and s.layers_positive == 1
    assert verdict(s, ctx) != "link"
    assert verdict(score_pair(ctx, "c1/lander", "c2/lander", Relation.operator), ctx) == "link"


def test_shared_aio_campaign_opens_the_gate_whatever_wins_its_slot():
    """HACK-53 review: two landings of one aio campaign sharing the campaign id and a campaign-constant kit set:
    the gate reads the candidates before the chain rule, so the pair links whichever row carries the level."""
    chain = [
        ind(L.platform, "platform_name", "stack-a"),
        ind(L.tenant, "aio_cdn_tenant", "tenant-a"),
        ind(L.owner, "aio_owner", "owner-a"),
        ind(L.owner, "aio_campaign", "campaign-a"),
        ind(L.kit, "kit_asset_set", "kitset-a", live=True),
        ind(L.tracking_account, "meta_pixel", "pixel-a", live=True),
    ]
    other = [
        ind(L.platform, "platform_name", "stack-a"),
        ind(L.tenant, "aio_cdn_tenant", "tenant-a"),
        ind(L.owner, "aio_owner", "owner-a"),
        ind(L.owner, "aio_campaign", "campaign-b"),
        ind(L.kit, "kit_asset_set", "kitset-b", live=True),
        ind(L.tracking_account, "meta_pixel", "pixel-a", live=True),
    ]
    fillers = [incident(f"f{i}", [ind(L.landing, "aio_landing", f"landing-f{i}")]) for i in range(10)]
    ds = Dataset(name="chain", incidents=[
        incident("c1", chain + [ind(L.landing, "aio_landing", "landing-1")]),
        incident("c2", chain + [ind(L.landing, "aio_landing", "landing-2")]),
        incident("c3", chain + [ind(L.landing, "aio_landing", "landing-2")]),
        incident("o1", other + [ind(L.landing, "aio_landing", "landing-3")]),
    ] + fillers)
    ctx = ScoringContext.from_dataset(ds)
    same = score_pair(ctx, "c1/lander", "c2/lander", Relation.campaign)
    assert same.live_private  # the gate keeps the live private id: a shared aio campaign is campaign evidence
    assert verdict(same, ctx) == "link"
    # the other campaign of the same owner: aio campaign conflict, only owner-level evidence left
    assert verdict(score_pair(ctx, "c1/lander", "o1/lander", Relation.campaign), ctx) != "link"


def test_owner_campaigns_grouping():
    """End to end: the two campaigns of one owner are one operator group and two campaign groups."""
    from ftf.linking import link_dataset

    out = link_dataset(_owner_campaigns(), relations=[Relation.campaign, Relation.operator])

    def together(relation, a, b):
        return any(a in c.capture_ids and b in c.capture_ids for c in out.campaigns if c.relation is relation)

    assert together(Relation.campaign, "c1/lander", "c1b/lander")
    assert not together(Relation.campaign, "c1/lander", "c2/lander")
    assert together(Relation.operator, "c1/lander", "c2/lander")


def test_verdict_needs_two_layers_or_live_private():
    from ftf.linking.types import PairScore
    ctx = ScoringContext.from_dataset(Dataset(name="tiny", incidents=[incident("x", []), incident("y", [])]))
    one = PairScore(a="x/lander", b="y/lander", relation=Relation.campaign, bits=20.0, layers_positive=1)
    assert verdict(one, ctx) == "review"
    assert verdict(PairScore(**{**one.__dict__, "live_private": True}), ctx) == "link"
    assert verdict(PairScore(**{**one.__dict__, "layers_positive": 2}), ctx) == "link"


def test_no_strict_level_with_tenant_conflict(ctx):
    from ftf.contract.models import EvidenceRow
    from ftf.linking.ledger import level_of
    from ftf.linking.types import PairScore
    conflict = EvidenceRow(layer=L.tenant, kind="aio_cdn_tenant", status=EvidenceStatus.conflict, bits=-4.0)
    s = PairScore(a="x", b="y", relation=Relation.campaign, bits=5.0, rows=[conflict], layers_positive=2, live_private=True)
    assert level_of(s) is LinkLevel.medium


def test_reseller_tenant_with_two_owners_is_cannot_link():
    """07 §2.2 item 5: one live tenant id hosting two owners does not make one campaign."""
    def side(owner: str) -> list[tuple]:
        return [
            ind(L.tenant, "aio_cdn_tenant", "tenant-z", live=True),
            ind(L.owner, "aio_owner", owner, live=True),
            ind(L.tracking_account, "meta_pixel", "pixel-z", live=True),
            ind(L.landing, "aio_landing", "landing-z"),
            ind(L.kit, "kit_asset_set", "kitset-z"),
        ]
    ds = Dataset(name="reseller", incidents=[incident("x", side("owner-1")), incident("y", side("owner-2")), incident("z", [])])
    ctx = ScoringContext.from_dataset(ds)
    assert ctx.group_of["x"] != ctx.group_of["y"]  # neither tenant nor pixel joins two owners
    for rel in (Relation.campaign, Relation.operator):
        s = score_pair(ctx, "x/lander", "y/lander", rel)
        # the shared tenant is explained away by the differing owners under it (chain rule, HACK-23)
        assert row(s, "aio_cdn_tenant").status is EvidenceStatus.hub_subsumed
        assert row(s, "aio_owner").status is EvidenceStatus.conflict
        assert row(s, "aio_cdn_tenant").seen_in.startswith("2 groups")
        assert verdict(s, ctx) != "link"
        assert to_link(s, "L", ctx).level is not LinkLevel.strict
    tmpl = score_pair(ctx, "x/lander", "y/lander", Relation.template)
    assert not [r for r in tmpl.rows if r.status is EvidenceStatus.conflict]
    assert verdict(tmpl, ctx) == "link"
    # same tenant, no owner ids on either side: the tenant joins them into one pre-group
    bare = [i for i in side("unused") if i[0] is not L.owner]
    ctx2 = ScoringContext.from_dataset(Dataset(name="tenant-only", incidents=[incident("x", bare), incident("y", bare)]))
    assert ctx2.group_of["x"] == ctx2.group_of["y"]
    # same static owner on both sides: the live tenant still joins them
    same = [i for i in side("unused") if i[0] is not L.owner] + [ind(L.owner, "aio_owner", "owner-1")]
    ctx3 = ScoringContext.from_dataset(Dataset(name="same-owner", incidents=[incident("x", same), incident("y", same)]))
    assert ctx3.group_of["x"] == ctx3.group_of["y"]


def test_collector_traces_change_no_score(ds, ctx):
    collector = [
        ind(L.collector, "aio_fingerprint", "f" * 32),
        ind(L.collector, "viewport", "1920x1080"),
        ind(L.collector, "capture_day", "2026-01-01"),
    ]
    incidents = []
    for inc in base_incidents():
        if inc.id in ("a1", "d"):
            extra = incident(inc.id, collector).indicators
            extra = [i.model_copy(update={"id": i.id + "/c"}) for i in extra]
            inc = inc.model_copy(update={"indicators": inc.indicators + extra})
        incidents.append(inc)
    ctx2 = ScoringContext.from_dataset(Dataset(name="with-collector", incidents=incidents))
    caps = [c.id for i in ds.incidents for c in i.captures]
    for x in caps:
        for y in caps:
            if x < y:
                for rel in Relation:
                    s1, s2 = score_pair(ctx, x, y, rel), score_pair(ctx2, x, y, rel)
                    assert (s1.bits, s1.layers_positive, s1.live_private) == (s2.bits, s2.layers_positive, s2.live_private)
    s = score_pair(ctx2, "a1/lander", "d/lander")
    excluded = [r for r in s.rows if r.status is EvidenceStatus.collector_excluded]
    assert {r.kind for r in excluded} == {"aio_fingerprint", "viewport", "capture_day"}
    assert all(r.bits == 0 for r in excluded)


def test_many_platform_constants_count_once():
    consts = [ind(L.platform, "aio_action_files", f"const-{i}") for i in range(10)]
    consts += [ind(L.platform, "uniqueizer_hue_rotate", "present"), ind(L.platform, "aio_sdk_asset", "sdk-9")]
    ds = Dataset(name="platform", incidents=[incident("x", consts), incident("y", consts), incident("z", [])])
    ctx = ScoringContext.from_dataset(ds)
    s = score_pair(ctx, "x/lander", "y/lander")
    assert 0 < s.bits <= HAND_TABLE.kinds["aio_sdk_asset"].bits
    assert s.layers_positive == 1
    assert sum(1 for r in s.rows if r.bits > 0) == 1


def test_missing_modality_is_zero_never_negative(ctx):
    ds = Dataset(name="missing", incidents=base_incidents() + [
        incident("m", [ind(L.creative, "ad_caption", "Unrelated placeholder caption")], stage="ad", cap="ad"),
    ])
    ctx = ScoringContext.from_dataset(ds)
    s = score_pair(ctx, "m/ad", "a1/lander")
    assert s.bits == 0
    missing = [r for r in s.rows if r.status is EvidenceStatus.missing]
    assert {r.layer for r in missing} >= {L.tenant, L.landing, L.tracking_account}
    assert all(r.bits == 0 for r in s.rows)


def test_duplicates_are_collapsed(ds, ctx):
    dup_inds = [ind(*i[:3], live=i[3]) for i in CAMPAIGN_A]
    ds2 = Dataset(name="dup", incidents=base_incidents() + [incident("dup", dup_inds, dup="a1/lander")])
    ctx2 = ScoringContext.from_dataset(ds2)
    assert ctx2.n == ctx.n
    s = score_pair(ctx, "n/lander", "a1/lander")
    s2 = score_pair(ctx2, "n/lander", "dup/lander")
    assert s2.b == "a1/lander"
    assert s2.bits == s.bits
    with pytest.raises(ValueError):
        score_pair(ctx2, "a1/lander", "dup/lander")
    # the link cites only indicators of its two incidents and validates inside the dataset
    link = to_link(s2, "L-dup", ctx2)
    Dataset(name="dup-linked", incidents=ds2.incidents, links=[link])


def test_to_link_levels_and_validation(ds, ctx):
    links = [
        to_link(score_pair(ctx, "n/lander", "a1/lander"), "L1", ctx),
        to_link(score_pair(ctx, "k1/lander", "k2/lander", Relation.template), "L2", ctx),
        to_link(score_pair(ctx, "d/lander", "b1/lander"), "L3", ctx),
    ]
    assert [l.level for l in links] == [LinkLevel.strict, LinkLevel.loose, LinkLevel.loose]
    assert all(l.method == "ledger@0.1" for l in links)
    Dataset.model_validate_json(Dataset(name="linked", incidents=ds.incidents, links=links).to_json())


def test_threshold_grows_with_n(ctx):
    assert threshold(ctx, Relation.campaign) == pytest.approx(3 + __import__("math").log2(12 / 3), abs=1e-3)
    big = Dataset(name="big", incidents=[incident(f"i{k}", []) for k in range(3000)])
    assert threshold(ScoringContext.from_dataset(big), Relation.campaign) > threshold(ctx, Relation.campaign) + 5


def test_unknown_values_ignored():
    inds = [ind(L.infrastructure, "a_record_ip", "unknown")]
    ds = Dataset(name="unk", incidents=[incident("x", inds), incident("y", inds)])
    s = score_pair(ScoringContext.from_dataset(ds), "x/lander", "y/lander")
    assert s.bits == 0 and s.rows == []


def test_empty_values_and_id_case():
    ds = Dataset(name="norm", incidents=[
        incident("x", [ind(L.creative, "page_title", "  "), ind(L.landing, "aio_landing", "Xq7aB"),
                       ind(L.creative, "ad_caption", "Same Caption")]),
        incident("y", [ind(L.creative, "page_title", ""), ind(L.landing, "aio_landing", "xq7ab"),
                       ind(L.creative, "ad_caption", "same  caption")]),
        incident("z", []),
    ])
    s = score_pair(ScoringContext.from_dataset(ds), "x/lander", "y/lander")
    assert not [r for r in s.rows if r.kind == "page_title"]  # empty titles are no evidence
    assert not [r for r in s.rows if r.status is EvidenceStatus.shared and r.kind == "aio_landing"]  # ids keep case
    assert row(s, "ad_caption").status is EvidenceStatus.shared  # free text folds case


def test_missing_row_sees_vector_only_layer():
    from ftf.contract.models import VectorRef
    x = incident("x", [ind(L.creative, "ad_caption", "Caption")])
    y = incident("y", [])
    y = y.model_copy(update={"indicators": [Indicator(
        id="y/lander/emb", capture_id="y/lander", layer=L.creative, kind="text_embedding", level=Level.campaign,
        vector_ref=VectorRef(store="s", key="k", model="m", dim=4), observed=Observed.static, extractor="synthetic@0",
    )]})
    s = score_pair(ScoringContext.from_dataset(Dataset(name="vec", incidents=[x, y])), "x/lander", "y/lander")
    assert not [r for r in s.rows if r.status is EvidenceStatus.missing]


def test_cli_score_bad_input(tmp_path, ds, capsys):
    path = tmp_path / "ds.json"
    path.write_text(ds.to_json())
    assert main(["score", str(path), "n/lander", "nope"]) == 2
    assert "unknown capture nope" in capsys.readouterr().err
    assert main(["score", str(tmp_path / "absent.json"), "a", "b"]) == 2


def test_cli_score(tmp_path, ds, capsys):
    path = tmp_path / "ds.json"
    path.write_text(ds.to_json())
    assert main(["score", str(path), "n/lander", "a1/lander"]) == 0
    out = capsys.readouterr().out
    assert "aio_cdn_tenant" in out and "verdict=link" in out and "level=strict" in out
    assert main(["score", str(path), "k1/lander", "k2/lander", "--relation", "template"]) == 0
    assert "relation=template" in capsys.readouterr().out
