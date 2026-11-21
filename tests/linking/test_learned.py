"""Learned stability, background rarity and the id hierarchy, on synthetic captures (placeholder values only)."""

from __future__ import annotations

import json

import pytest

from ftf.cli import main
from ftf.contract.models import Capture, Dataset, EvidenceStatus, Incident, Indicator, Layer, Level, Observed, Relation
from ftf.linking import HAND_TABLE, ScoringContext, score_pair
from ftf.linking.background import BackgroundCounts
from ftf.linking.hierarchy import Hierarchy
from ftf.linking.learned import bits_from_s, learn_weights, load_weights, dump_weights, s_from_bits

L = Layer


def ind(layer: Layer, kind: str, value: str, live: bool = False) -> tuple:
    return (layer, kind, value, live)


def incident(iid: str, inds: list[tuple]) -> Incident:
    cid = f"{iid}/lander"
    return Incident(
        id=iid, source="synthetic", captures=[Capture(id=cid, stage="lander")],
        indicators=[
            Indicator(
                id=f"{cid}/{kind}/{n}", capture_id=cid, layer=layer, kind=kind, level=Level.campaign, value=value,
                observed=Observed.live if live else Observed.static, extractor="synthetic@0",
            )
            for n, (layer, kind, value, live) in enumerate(inds)
        ],
    )


def chain(tenant: str, owner: str, campaign: str | None = None, landing: str | None = None) -> list[tuple]:
    out = [ind(L.tenant, "aio_cdn_tenant", tenant, live=True), ind(L.owner, "aio_owner", owner, live=True)]
    if campaign:
        out += [ind(L.owner, "aio_campaign", campaign), ind(L.owner, "aio_campaign_name", f"name-{campaign}")]
    if landing:
        out.append(ind(L.landing, "aio_landing", landing))
    return out


PLATFORM = [ind(L.platform, "platform_name", "stack-a"), ind(L.platform, "aio_sdk_asset", "sdk-const-1")]
EXTRA_CONSTANTS = [
    ind(L.kit, "aio_macros_file", "macros-const"),
    ind(L.kit, "kit_asset_set", "kitset-const"),
    ind(L.kit, "asset_sha256", "asset-const"),
    ind(L.kit, "inline_script_sha256", "script-const"),
    ind(L.kit, "aio_lander_template", "template-const"),
    ind(L.platform, "aio_action_files", "actions-const"),
]


def platform_ds(constants: list[tuple]) -> Dataset:
    return Dataset(name="synthetic", incidents=[
        incident("x1", constants + chain("tenant-1", "owner-1", "camp-1", "landing-1")),
        incident("x2", constants + chain("tenant-2", "owner-2", "camp-2", "landing-2")),
    ])


def platform_bits(score, kinds: set[str]) -> list[float]:
    return [r.bits for r in score.rows if r.kind in kinds and r.status is EvidenceStatus.shared and r.bits > 0]


# ---- acceptance 1: a shared platform is one small term ----


def test_shared_platform_is_one_small_term_however_many_constants():
    kinds = {c[1] for c in PLATFORM + EXTRA_CONSTANTS}
    few = score_pair(ScoringContext.from_dataset(platform_ds(PLATFORM)), "x1/lander", "x2/lander")
    many = score_pair(ScoringContext.from_dataset(platform_ds(PLATFORM + EXTRA_CONSTANTS)), "x1/lander", "x2/lander")
    assert len(platform_bits(few, kinds)) == 1 and len(platform_bits(many, kinds)) == 1
    assert sum(platform_bits(many, kinds)) == pytest.approx(sum(platform_bits(few, kinds)))
    assert many.bits == pytest.approx(few.bits)
    assert sum(platform_bits(many, kinds)) <= HAND_TABLE.kinds["platform_name"].bits
    const_rows = [r for r in many.rows if r.kind in kinds]
    assert len(const_rows) == len(kinds)  # every constant itemised: one term, the rest 0 bits and explained
    chained = [r for r in const_rows if r.status is EvidenceStatus.hub_subsumed and "chain rule" in r.note]
    assert len(chained) >= 2 and all(r.bits == 0 and "platform" in r.note for r in chained)
    # without the chain rule the same constants add up across layers
    off = score_pair(ScoringContext.from_dataset(platform_ds(PLATFORM + EXTRA_CONSTANTS), chain_rule=False),
                     "x1/lander", "x2/lander")
    assert sum(platform_bits(off, kinds)) > sum(platform_bits(many, kinds)) + 2
    assert sum(r.bits for r in many.rows) == pytest.approx(many.bits, abs=1e-6)


def test_platform_constant_in_another_layer_is_capped_without_a_platform_row():
    consts = [ind(L.kit, "kit_asset_set", "kitset-const"), ind(L.kit, "asset_sha256", "asset-const")]
    score = score_pair(ScoringContext.from_dataset(platform_ds(consts)), "x1/lander", "x2/lander")
    kept = platform_bits(score, {"kit_asset_set", "asset_sha256"})
    assert len(kept) == 1 and kept[0] <= HAND_TABLE.kinds["platform_name"].bits
    # an operator-private id across two tenants is never demoted to a platform constant
    private = [ind(L.offer, "contact_email", "desk@mail.example")]
    s2 = score_pair(ScoringContext.from_dataset(platform_ds(PLATFORM + private)), "x1/lander", "x2/lander")
    (email,) = [r for r in s2.rows if r.kind == "contact_email"]
    assert email.status is EvidenceStatus.shared and email.bits > 0


# ---- hierarchy discovery and level-level subsumption ----


def hierarchy_ds() -> Dataset:
    return Dataset(name="synthetic", incidents=[
        incident("a1", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [
            ind(L.creative, "ad_caption", "Caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]),
        incident("a2", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [
            ind(L.creative, "ad_caption", "Caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]),
        incident("a3", PLATFORM + chain("tenant-1", "owner-1", "camp-2", "landing-3") + [
            ind(L.tracking_account, "meta_pixel", "pixel-1", live=True), ind(L.tenant, "aio_handle_key", "handle-1")]),
        incident("r1", PLATFORM + chain("tenant-2", "owner-2", "camp-3", "landing-4") + [ind(L.tenant, "aio_handle_key", "handle-2")]),
        incident("r2", PLATFORM + chain("tenant-2", "owner-3", "camp-4", "landing-5") + [ind(L.tenant, "aio_handle_key", "handle-2")]),
    ])


def test_levels_and_level_graph():
    h = Hierarchy.from_dataset(hierarchy_ds())
    lv = {kind: h.level((layer, kind, value)) for layer, kind, value, _ in
          [ind(L.creative, "ad_caption", "caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1"),
           ind(L.platform, "aio_sdk_asset", "sdk-const-1"), ind(L.tenant, "aio_handle_key", "handle-2"),
           ind(L.landing, "aio_landing", "landing-1")]}
    assert lv == {"ad_caption": None, "meta_pixel": Level.operator, "aio_sdk_asset": Level.platform,
                  "aio_handle_key": Level.tenant, "aio_landing": Level.landing}
    assert h.parents == {Level.landing: Level.campaign, Level.campaign: Level.operator,
                         Level.operator: Level.tenant, Level.tenant: None}
    assert h.summary()["level_graph"]["landing"] == "aio campaign"


def test_values_explained_by_a_shared_level_id_count_zero():
    ctx = ScoringContext.from_dataset(hierarchy_ds())
    # a caption inside one landing is no constant: it keeps its weight next to the landing id
    same = score_pair(ctx, "a1/lander", "a2/lander")
    (caption,) = [r for r in same.rows if r.kind == "ad_caption"]
    assert caption.status is EvidenceStatus.shared and caption.bits > 0
    s = score_pair(ctx, "a1/lander", "a3/lander", Relation.operator)
    (pixel,) = [r for r in s.rows if r.kind == "meta_pixel"]
    assert pixel.status is EvidenceStatus.hub_subsumed and pixel.bits == 0 and "owner" in pixel.note
    (owner,) = [r for r in s.rows if r.kind == "aio_owner"]
    assert owner.status is EvidenceStatus.shared and owner.bits > 0
    # a reseller's handle key spanning two owners of one tenant is tenant-level, explained by the shared tenant
    s2 = score_pair(ctx, "r1/lander", "r2/lander", Relation.operator)
    (handle,) = [r for r in s2.rows if r.kind == "aio_handle_key"]
    assert handle.status is EvidenceStatus.hub_subsumed and "tenant" in handle.note


# ---- acceptance 2: rarity over operators, not incidents ----


def big_campaign_ds(size: int = 8) -> Dataset:
    big = [incident(f"big{i}", chain("tenant-1", "owner-big", "camp-big", "landing-big") +
                    [ind(L.creative, "ad_caption", "Big caption")]) for i in range(size)]
    small = [incident(f"s{i}", chain("tenant-1", f"owner-s{i}", f"camp-s{i}", f"landing-s{i}") +
                      [ind(L.creative, "ad_caption", "Small caption")]) for i in range(2)]
    singles = [incident(f"z{i}", chain(f"tenant-z{i}", f"owner-z{i}", f"camp-z{i}", f"landing-z{i}")) for i in range(5)]
    return Dataset(name="synthetic", incidents=big + small + singles)


def test_background_counts_operators_not_incidents():
    bg = BackgroundCounts.from_dataset(big_campaign_ds())
    assert bg.n_incidents == 15 and bg.n_operators == 8
    assert bg.count(L.landing, "aio_landing", "landing-big") == 1
    assert bg.count(L.creative, "ad_caption", "big caption") == 1
    assert bg.count(L.creative, "ad_caption", "small caption") == 2  # two operators
    assert bg.u(L.landing, "aio_landing", "landing-big") == bg.u(L.landing, "aio_landing", "landing-z0")


def test_large_campaign_ids_are_not_down_weighted():
    ds = big_campaign_ds()
    other = BackgroundCounts.from_dataset(Dataset(name="bg", incidents=[
        incident(f"q{i}", chain(f"tenant-q{i}", f"owner-q{i}", f"camp-q{i}", f"landing-q{i}") +
                 [ind(L.creative, "ad_caption", "Small caption")]) for i in range(6)
    ]))
    for background in (None, other):
        ctx = ScoringContext.from_dataset(ds, background=background, chain_rule=False)
        big = score_pair(ctx, "big0/lander", "big7/lander")
        (landing,) = [r for r in big.rows if r.kind == "aio_landing"]
        assert landing.bits == pytest.approx(HAND_TABLE.kinds["aio_landing"].bits)  # no rarity deduction
        (cap,) = [r for r in big.rows if r.kind == "ad_caption"]
        assert cap.bits == pytest.approx(HAND_TABLE.kinds["ad_caption"].bits)
    # a value of several operators (in the corpus) does lose bits
    ctx = ScoringContext.from_dataset(ds, background=other, chain_rule=False)
    (cap,) = [r for r in score_pair(ctx, "s0/lander", "s1/lander").rows if r.kind == "ad_caption"]
    assert cap.bits <= HAND_TABLE.kinds["ad_caption"].bits - 2


def test_learned_rarity_ignores_campaign_size():
    _, r8 = learn_weights(big_campaign_ds(8))
    _, r2 = learn_weights(big_campaign_ds(2))
    assert r8["kinds"]["aio_landing"]["u_ref"] == r2["kinds"]["aio_landing"]["u_ref"]


# ---- learned stability ----


def learning_ds(extra_pairs: int = 0) -> Dataset:
    incs = []
    for i in range(4):  # campaign 1 of owner 1: landing always shared, caption on half
        incs.append(incident(f"c1-{i}", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [
            ind(L.creative, "ad_caption", "Caption A" if i < 2 else f"Caption A{i}"),
            ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]))
    for i in range(2):  # campaign 2 of owner 1: other landing, same pixel
        incs.append(incident(f"c2-{i}", PLATFORM + chain("tenant-1", "owner-1", "camp-2", "landing-2") + [
            ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]))
    for i in range(3 + extra_pairs):  # owner 2
        incs.append(incident(f"c3-{i}", PLATFORM + chain("tenant-2", "owner-2", "camp-3", "landing-3") + [
            ind(L.tracking_account, "meta_pixel", "pixel-2", live=True)]))
    incs.append(incident("solo", PLATFORM + chain("tenant-3", "owner-3", "camp-4", "landing-4")))
    return Dataset(name="synthetic", incidents=incs)


def test_label_layer_is_held_out():
    _, rep = learn_weights(learning_ds())
    camp = rep["relations"]["campaign"]
    assert camp["label_kind"] == "aio_campaign" and camp["held_out_layers"] == ["owner"]
    assert camp["layers"]["owner"] == {"held_out": True}
    assert not {"aio_campaign", "aio_campaign_name", "aio_owner"} & set(camp["kinds"])
    assert rep["relations"]["operator"]["held_out_layers"] == ["owner"]
    assert "operator" not in rep["kinds"].get("aio_campaign_name", {}).get("learned_relations", [])
    assert "aio_campaign" not in rep["relations"]["operator"]["kinds"]


def test_persistence_estimates_and_weights():
    table, rep = learn_weights(learning_ds())
    camp = rep["relations"]["campaign"]
    assert camp["n_groups"] == 3 and camp["kinds"]["aio_landing"]["n_pos_shared"] == camp["kinds"]["aio_landing"]["n_pos"]
    assert camp["kinds"]["aio_landing"]["s"] > camp["kinds"]["ad_caption"]["s"]
    # pixel persists inside an owner across campaigns
    assert rep["relations"]["operator"]["kinds"]["meta_pixel"]["n_pos_shared"] > 0
    landing = table.kinds["aio_landing"]
    assert set(rep["kinds"]["aio_landing"]["learned_relations"]) == {"campaign", "operator"}
    assert landing.relations[Relation.template] * landing.bits == pytest.approx(
        HAND_TABLE.kinds["aio_landing"].bits * HAND_TABLE.scale("aio_landing", L.landing, Relation.template), abs=1e-2)
    # untouched kinds fall back to the hand table
    assert table.kinds["cert_issuer"] == HAND_TABLE.kinds["cert_issuer"]
    assert table.kinds["meta_campaign_id"] == HAND_TABLE.kinds["meta_campaign_id"]


def test_shrinkage_toward_prior_with_few_positives():
    def one_pair_ds() -> Dataset:
        return Dataset(name="synthetic", incidents=[
            incident("p1", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [ind(L.creative, "ad_caption", "Cap")]),
            incident("p2", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [ind(L.creative, "ad_caption", "Cap")]),
            incident("q1", PLATFORM + chain("tenant-2", "owner-2", "camp-2", "landing-2") + [ind(L.creative, "ad_caption", "Other")]),
        ])
    _, rep = learn_weights(one_pair_ds())
    li = rep["relations"]["campaign"]["layers"]["creative"]
    assert li["n_pos"] == 1 and li["shrinkage_weight"] == pytest.approx(1 / 5)
    assert abs(li["s"] - li["s_prior"]) < abs(1.0 - li["s"])  # closer to the prior than to the observed 1/1
    _, big = learn_weights(learning_ds(extra_pairs=20))
    assert big["relations"]["campaign"]["layers"]["landing"]["shrinkage_weight"] > 0.8


def test_bits_s_inverse():
    for s in (0.1, 0.5, 0.9):
        assert s_from_bits(bits_from_s(s, 0.05), 0.05) == pytest.approx(s)


# ---- JSON round trip and CLI ----


def test_json_round_trip_and_cli(tmp_path):
    ds = learning_ds()
    bg_ds = big_campaign_ds()
    table, rep = learn_weights(ds, background=BackgroundCounts.from_dataset(bg_ds))
    path = tmp_path / "weights.json"
    bg = BackgroundCounts.from_dataset(bg_ds)
    dump_weights(path, table, rep, bg)
    t2, bg2, rep2 = load_weights(path)
    assert t2 == table and rep2 == json.loads(json.dumps(rep))
    assert bg2.counts == bg.counts and bg2.kinds == bg.kinds and bg2.n_operators == bg.n_operators
    text = path.read_text()
    for placeholder in ("landing-big", "pixel-1", "Caption A", "owner-1", "camp-1"):
        assert placeholder not in text  # values are hashed, never written

    ds_path, bg_path, out = tmp_path / "ds.json", tmp_path / "bg.json", tmp_path / "w.json"
    ds_path.write_text(ds.to_json())
    bg_path.write_text(bg_ds.to_json())
    assert main(["learn-weights", str(ds_path), "--background", str(bg_path), "-o", str(out)]) == 0
    assert main(["score", str(ds_path), "c1-0/lander", "c1-1/lander", "--weights", str(out)]) == 0
    assert main(["score", str(ds_path), "c1-0/lander", "c1-1/lander", "--weights", str(tmp_path / "nope.json")]) == 2


# ---- one term per level inside a chain layer; a different aio campaign of one owner ----


def owner_campaign_ds() -> Dataset:
    def inc(iid, camp, landing):
        i = incident(iid, PLATFORM + chain("tenant-1", "owner-1", camp, landing))
        return i.model_copy(update={"indicators": [
            x.model_copy(update={"level": Level.operator}) if x.kind == "aio_owner" else x for x in i.indicators]})
    return Dataset(name="synthetic", incidents=[
        inc("m1", "camp-1", "landing-1"), inc("m2", "camp-1", "landing-1"), inc("m3", "camp-2", "landing-2"),
        incident("o1", PLATFORM + chain("tenant-9", "owner-9", "camp-9", "landing-9")),
    ])


def test_shared_owner_and_campaign_are_two_terms_and_alias_is_not():
    ctx = ScoringContext.from_dataset(owner_campaign_ds())
    s = score_pair(ctx, "m1/lander", "m2/lander")
    owner_layer = {r.kind: r for r in s.rows if r.layer is L.owner}
    assert owner_layer["aio_owner"].bits > 0
    campaign_terms = sorted(owner_layer[k].bits for k in ("aio_campaign", "aio_campaign_name"))
    assert campaign_terms[0] == 0 and campaign_terms[1] > 0  # the campaign level counts once


def test_a_different_aio_campaign_is_a_campaign_conflict_but_no_cannot_link():
    from ftf.linking.ledger import has_private_conflict

    table, rep = learn_weights(owner_campaign_ds())
    entry = table.kinds["aio_campaign"]
    assert entry.single_valued and entry.relations[Relation.operator] == 0
    ctx = ScoringContext.from_dataset(owner_campaign_ds(), weights=table)
    camp = score_pair(ctx, "m1/lander", "m3/lander", Relation.campaign)
    assert any(r.kind == "aio_campaign" and r.status is EvidenceStatus.conflict for r in camp.rows)
    assert not has_private_conflict(camp)  # cannot-link stays tenant/owner ids
    hand = score_pair(ScoringContext.from_dataset(owner_campaign_ds()), "m1/lander", "m3/lander", Relation.campaign)
    assert any(r.kind == "aio_campaign" and r.status is EvidenceStatus.conflict for r in hand.rows)
    op = score_pair(ctx, "m1/lander", "m3/lander", Relation.operator)
    assert not any(r.status is EvidenceStatus.conflict for r in op.rows)


def test_corpus_filters_certpl_and_uses_real_pages_only_as_brand_stop_list():
    corpus = big_campaign_ds()
    certpl = Incident(id="certpl:listing.example:listing", source="scanner_certpl",
                      captures=[Capture(id="certpl:listing.example:listing:c1", stage="unknown")])
    corpus = corpus.model_copy(update={"incidents": corpus.incidents + [certpl]})
    real = Dataset(name="real", incidents=[incident("outlet", [ind(L.creative, "ad_caption", "Small caption")])])
    bg = BackgroundCounts.from_dataset(corpus, must_not_link=real)
    assert bg.n_incidents == 15 and "scanner_certpl" not in bg.sources  # certpl listings left out
    assert bg.n_operators == 8  # real pages are not in the denominator
    assert bg.is_brand(L.creative, "ad_caption", "small caption")
    ctx = ScoringContext.from_dataset(big_campaign_ds(), background=bg, chain_rule=False)
    (cap,) = [r for r in score_pair(ctx, "s0/lander", "s1/lander").rows if r.kind == "ad_caption"]
    assert cap.status is EvidenceStatus.hub_subsumed and "brand skin" in cap.note
    assert BackgroundCounts.from_json(json.loads(json.dumps(bg.to_json()))).brand == bg.brand


# ---- review fixes ----


def test_template_relation_keeps_cross_tenant_kit_evidence():
    ds = platform_ds(PLATFORM + [ind(L.kit, "kit_asset_set", "kitset-const")])
    on = score_pair(ScoringContext.from_dataset(ds), "x1/lander", "x2/lander", Relation.template)
    off = score_pair(ScoringContext.from_dataset(ds, chain_rule=False), "x1/lander", "x2/lander", Relation.template)
    assert on.bits == off.bits > 0


def test_values_on_captures_without_tenant_or_owner_are_no_platform_constants():
    ds = Dataset(name="synthetic", incidents=[
        incident("l1", [ind(L.landing, "aio_landing", "landing-1"), ind(L.kit, "kit_asset_set", "kitset-1")]),
        incident("l2", [ind(L.landing, "aio_landing", "landing-2"), ind(L.kit, "kit_asset_set", "kitset-1")]),
    ])
    assert Hierarchy.from_dataset(ds).level((L.kit, "kit_asset_set", "kitset-1")) is None


def test_live_private_gate_ignores_ids_zeroed_by_the_chain_rule():
    def inc(iid, camp):
        cid = f"{iid}/lander"
        rows = [(L.tenant, "aio_cdn_tenant", "tenant-1", Level.tenant, False),
                (L.owner, "aio_owner", "owner-1", Level.operator, False),
                (L.owner, "aio_campaign", camp, Level.campaign, False),
                (L.tracking_account, "meta_pixel", "pixel-1", Level.operator, True)]
        return Incident(id=iid, source="synthetic", captures=[Capture(id=cid, stage="lander")], indicators=[
            Indicator(id=f"{cid}/{n}", capture_id=cid, layer=ly, kind=k, level=lv, value=v,
                      observed=Observed.live if live else Observed.static, extractor="synthetic@0")
            for n, (ly, k, v, lv, live) in enumerate(rows)])
    ds = Dataset(name="synthetic", incidents=[inc("g1", "camp-1"), inc("g2", "camp-2"), inc("g3", "camp-2")])
    s = score_pair(ScoringContext.from_dataset(ds), "g1/lander", "g2/lander", Relation.operator)
    (pixel,) = [r for r in s.rows if r.kind == "meta_pixel"]
    assert pixel.status is EvidenceStatus.hub_subsumed and not s.live_private


def test_a_page_shape_next_to_the_landing_id_is_not_a_second_landing_term():
    shape = ind(L.landing, "page_url_shape", "/x/{id}/y")
    ds = Dataset(name="synthetic", incidents=[
        incident("w1", chain("tenant-1", "owner-1", "camp-1", "landing-1") + [shape]),
        incident("w2", chain("tenant-1", "owner-1", "camp-1", "landing-1") + [shape]),
    ])
    s = score_pair(ScoringContext.from_dataset(ds), "w1/lander", "w2/lander")
    landing_terms = [r.bits for r in s.rows if r.layer is L.landing and r.bits > 0]
    assert len(landing_terms) == 1


def test_kind_without_own_positive_pairs_keeps_the_prior_entry():
    ds = learning_ds()
    extra = incident("lonely", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") +
                     [ind(L.tracking_account, "google_ads_id", "ads-1")])
    other = incident("lonely2", PLATFORM + chain("tenant-3", "owner-3", "camp-4", "landing-4") +
                     [ind(L.tracking_account, "google_ads_id", "ads-1")])
    table, rep = learn_weights(ds.model_copy(update={"incidents": ds.incidents + [extra, other]}))
    assert table.kinds["google_ads_id"] == HAND_TABLE.kinds["google_ads_id"]
    assert rep["kinds"]["google_ads_id"]["fallback"] == "no positive pairs"


def test_corpus_containing_the_sample_is_not_counted_twice():
    ds = big_campaign_ds()
    ctx = ScoringContext.from_dataset(ds, background=BackgroundCounts.from_dataset(ds), chain_rule=False)
    (landing,) = [r for r in score_pair(ctx, "big0/lander", "big7/lander").rows if r.kind == "aio_landing"]
    assert landing.bits == pytest.approx(HAND_TABLE.kinds["aio_landing"].bits)


def test_link_with_a_bad_weights_file_exits_cleanly(tmp_path):
    ds_path = tmp_path / "ds.json"
    ds_path.write_text(learning_ds().to_json())
    assert main(["link", str(ds_path), "-o", str(tmp_path / "o.json"), "--weights", str(tmp_path / "nope.json")]) == 2


def test_multi_valued_kind_is_capped_at_log2_operators_and_keeps_the_copy_prior():
    import math

    def many_assets(iid, tenant, owner, camp, landing, shared):
        own = [ind(L.kit, "asset_sha256", f"{iid}-asset-{n}") for n in range(40)]
        return incident(iid, PLATFORM + chain(tenant, owner, camp, landing) + own +
                        [ind(L.kit, "asset_sha256", shared)])
    ds = Dataset(name="synthetic", incidents=[
        many_assets("k1", "tenant-1", "owner-1", "camp-1", "landing-1", "pack-1"),
        many_assets("k2", "tenant-1", "owner-1", "camp-1", "landing-1", "pack-1"),
        many_assets("k3", "tenant-2", "owner-2", "camp-2", "landing-2", "pack-2"),
        many_assets("k4", "tenant-2", "owner-2", "camp-2", "landing-2", "pack-2"),
    ])
    table, rep = learn_weights(ds)
    info = rep["kinds"]["asset_sha256"]
    assert info["V"] > 100 and info["u_ref"] >= 1 / info["N"]
    assert info["bits"]["campaign"] <= math.log2(info["N"]) * HAND_TABLE.scale("asset_sha256", L.kit, Relation.campaign) + 1e-6


def test_score_warns_about_kinds_without_a_weight_entry(tmp_path, capsys):
    ds = Dataset(name="synthetic", incidents=[
        incident("u1", [ind(L.kit, "made_up_kind", "v-1"), ind(L.tenant, "aio_cdn_tenant", "tenant-1")]),
        incident("u2", [ind(L.kit, "made_up_kind", "v-1"), ind(L.tenant, "aio_cdn_tenant", "tenant-1")]),
    ])
    path = tmp_path / "ds.json"
    path.write_text(ds.to_json())
    assert main(["score", str(path), "u1/lander", "u2/lander"]) == 0
    err = capsys.readouterr().err
    assert "made_up_kind (2)" in err and "aio_cdn_tenant" not in err


# ---- review round 2 ----


def test_carriers_without_chain_ids_make_a_value_no_chain_constant():
    pixel = ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)
    ds = Dataset(name="synthetic", incidents=[
        incident("v1", chain("tenant-1", "owner-1", "camp-1", "landing-1") + [pixel]),
        incident("v2", chain("tenant-1", "owner-1", "camp-1", "landing-2") + [pixel]),
        incident("v3", [ind(L.platform, "platform_name", "stack-z"), pixel]),
    ])
    assert Hierarchy.from_dataset(ds).level((L.tracking_account, "meta_pixel", "pixel-1")) is None


def test_one_term_per_level_keeps_the_stronger_of_id_and_constant():
    def inc(iid, camp):
        cid = f"{iid}/lander"
        rows = [(L.tenant, "aio_cdn_tenant", "tenant-1", Level.tenant, True),
                (L.owner, "aio_owner", "owner-1", Level.operator, False),  # static: 6 − 1 copy prior
                (L.owner, "aio_campaign", camp, Level.campaign, False),
                (L.tracker_instance, "xid_tracker_machine", "xid-1", Level.operator, True)]
        return Incident(id=iid, source="synthetic", captures=[Capture(id=cid, stage="lander")], indicators=[
            Indicator(id=f"{cid}/{n}", capture_id=cid, layer=ly, kind=k, level=lv, value=v,
                      observed=Observed.live if live else Observed.static, extractor="synthetic@0")
            for n, (ly, k, v, lv, live) in enumerate(rows)])
    ds = Dataset(name="synthetic", incidents=[inc("h1", "camp-1"), inc("h2", "camp-2")])
    s = score_pair(ScoringContext.from_dataset(ds), "h1/lander", "h2/lander", Relation.operator)
    rows = {r.kind: r for r in s.rows}
    level_terms = [rows["aio_owner"].bits, rows["xid_tracker_machine"].bits]
    assert sorted(level_terms)[0] == 0 and max(level_terms) == pytest.approx(
        max(HAND_TABLE.kinds["aio_owner"].bits - 1, HAND_TABLE.kinds["xid_tracker_machine"].bits))


def test_platform_term_never_opens_the_live_gate():
    ds = Dataset(name="synthetic", incidents=[
        incident(f"t{i}", chain(f"tenant-{i}", f"owner-{i}") + [ind(L.kit, "asset_sha256", "asset-const", live=True)])
        for i in range(3)
    ])
    s = score_pair(ScoringContext.from_dataset(ds), "t0/lander", "t1/lander", Relation.operator)
    (asset,) = [r for r in s.rows if r.kind == "asset_sha256"]
    assert asset.bits <= HAND_TABLE.kinds["platform_name"].bits
    assert not s.live_private or any(r.layer in (L.tenant, L.owner) and r.bits > 0 for r in s.rows)


def test_capacity_counts_owner_and_campaign_as_two_terms():
    from ftf.linking.backend import layer_caps

    caps = layer_caps(HAND_TABLE, Relation.campaign)
    assert caps[L.owner] == pytest.approx(HAND_TABLE.kinds["aio_owner"].bits + HAND_TABLE.kinds["aio_campaign"].bits)
    assert layer_caps(HAND_TABLE, Relation.template).get(L.owner, 0) <= HAND_TABLE.kinds["aio_owner"].bits


def test_corpus_kinds_keep_only_rarity_kinds_and_must_not_link_needs_background(tmp_path):
    ds = big_campaign_ds()
    kinds = {i.id: ("fetched_landing" if i.id.startswith("big") else "bare_domain") for i in ds.incidents}
    bg = BackgroundCounts.from_dataset(ds, corpus_kinds=kinds)
    assert bg.n_incidents == 8 and bg.n_operators == 1
    path = tmp_path / "ds.json"
    path.write_text(ds.to_json())
    assert main(["learn-weights", str(path), "--must-not-link", str(path), "-o", str(tmp_path / "w.json")]) == 2


def test_shared_domain_or_common_redirector_alone_does_not_link():
    from ftf.linking.ledger import verdict

    filler = [incident(f"f{i}", [ind(L.infrastructure, "landing_domain", f"other-{i}.example")]) for i in range(6)]
    two_domains = Dataset(name="synthetic", incidents=[
        incident("c1", [ind(L.infrastructure, "certpl_listing_domain", "same.example")]),
        incident("c2", [ind(L.infrastructure, "certpl_listing_domain", "same.example")]),
    ] + filler)
    ctx = ScoringContext.from_dataset(two_domains)
    assert verdict(score_pair(ctx, "c1/lander", "c2/lander"), ctx) != "link"
    redirector = [ind(L.tracker_instance, "chain_host", "redir.example", live=True),
                  ind(L.tracker_instance, "redirect_chain_shape", "a>b>c")]
    pages = Dataset(name="synthetic", incidents=[
        incident("r1", redirector + [ind(L.infrastructure, "landing_host", "one.example")]),
        incident("r2", redirector + [ind(L.infrastructure, "landing_host", "two.example")]),
    ] + filler)
    ctx = ScoringContext.from_dataset(pages)
    s = score_pair(ctx, "r1/lander", "r2/lander")
    assert verdict(s, ctx) == "no link" and not s.live_private
    assert not any(r.status is EvidenceStatus.conflict for r in s.rows)  # differing hosts carry nothing


def test_learned_bits_raise_a_prior_by_at_most_max_raise():
    from ftf.linking.learned import MAX_RAISE

    table, rep = learn_weights(learning_ds(extra_pairs=20))
    for kind, info in rep["kinds"].items():
        entry = HAND_TABLE.kinds.get(kind)
        if entry is None or not info["learned_relations"]:
            continue
        for rel in info["learned_relations"]:
            hand = entry.bits * HAND_TABLE.scale(kind, entry.layer, Relation(rel))
            assert info["bits"][rel] <= hand + MAX_RAISE + 1e-6, kind
