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
        # cluster B (the {48, 61}-like pair): same platform, own tenant, a creative pack and brand skin
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
        # (c) copied kit: same asset set, title and static (copied) pixel, different stacks
        incident("p8", [
            ind(L.platform, "tracker_script", "php-tracker-js"),
            ind(L.kit, "kit_asset_set", "kitset-x"),
            ind(L.creative, "page_title", "Placeholder title X"),
            ind(L.tracking_account, "meta_pixel", "pixel-x"),
        ]),
        incident("p11", AIO + [
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
    for a, b in [("n/lander", "a1/lander"), ("d/lander", "b1/lander"), ("p8/lander", "p11/lander")]:
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
    assert row(s, "aio_landing").seen_in == "3 incidents / 12"
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
    tmpl = score_pair(ctx, "p8/lander", "p11/lander", Relation.template)
    camp = score_pair(ctx, "p8/lander", "p11/lander", Relation.campaign)
    tau = threshold(ctx, Relation.campaign)
    assert tmpl.bits > 0
    assert row(tmpl, "kit_asset_set").bits > 0
    assert row(tmpl, "meta_pixel").bits == 0  # a pixel says nothing about the template
    assert tau - 3 <= camp.bits < tau
    assert verdict(camp, ctx) == "review"
    # pixel copied into saved HTML: static, so no strict link and a copy-prior discount
    assert not camp.live_private
    assert row(camp, "meta_pixel").bits < row(
        score_pair(ctx, "n/lander", "a1/lander", Relation.campaign), "meta_pixel"
    ).bits + HAND_TABLE.static_discount


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
        to_link(score_pair(ctx, "p8/lander", "p11/lander", Relation.template), "L2", ctx),
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


def test_cli_score(tmp_path, ds, capsys):
    path = tmp_path / "ds.json"
    path.write_text(ds.to_json())
    assert main(["score", str(path), "n/lander", "a1/lander"]) == 0
    out = capsys.readouterr().out
    assert "aio_cdn_tenant" in out and "verdict=link" in out and "level=strict" in out
    assert main(["score", str(path), "p8/lander", "p11/lander", "--relation", "template"]) == 0
    assert "relation=template" in capsys.readouterr().out
