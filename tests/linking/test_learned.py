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
    ind(L.kit, "aio_lander_template", "template-const"),
    ind(L.offer, "form_field_set", "fields-const"),
    ind(L.platform, "aio_action_files", "actions-const"),
    ind(L.kit, "kit_shared_script", "script-const"),
    ind(L.offer, "form_endpoint", "https://endpoint.example/submit"),
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
    subsumed = [r for r in many.rows if r.kind in kinds and r.status is EvidenceStatus.hub_subsumed]
    assert len(subsumed) == len(kinds) - 1 and all("platform" in r.note for r in subsumed)
    assert all(r.bits == 0 for r in subsumed)
    # without the chain rule the same constants add up across layers
    off = score_pair(ScoringContext.from_dataset(platform_ds(PLATFORM + EXTRA_CONSTANTS), chain_rule=False),
                     "x1/lander", "x2/lander")
    assert sum(platform_bits(off, kinds)) > sum(platform_bits(many, kinds)) + 2
    assert sum(r.bits for r in many.rows) == pytest.approx(many.bits, abs=1e-6)


def test_platform_constant_in_another_layer_is_capped_without_a_platform_row():
    consts = [ind(L.kit, "aio_lander_template", "template-const"), ind(L.offer, "form_field_set", "fields-const")]
    score = score_pair(ScoringContext.from_dataset(platform_ds(consts)), "x1/lander", "x2/lander")
    kept = platform_bits(score, {"aio_lander_template", "form_field_set"})
    assert len(kept) == 1 and kept[0] <= HAND_TABLE.kinds["platform_name"].bits


# ---- hierarchy discovery and level-level subsumption ----


def hierarchy_ds() -> Dataset:
    return Dataset(name="synthetic", incidents=[
        incident("a1", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [
            ind(L.creative, "ad_caption", "Caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]),
        incident("a2", PLATFORM + chain("tenant-1", "owner-1", "camp-1", "landing-1") + [
            ind(L.creative, "ad_caption", "Caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1", live=True)]),
        incident("a3", PLATFORM + chain("tenant-1", "owner-1", "camp-2", "landing-3") + [
            ind(L.tracking_account, "meta_pixel", "pixel-1", live=True), ind(L.tenant, "aio_flow", "flow-1")]),
        incident("r1", PLATFORM + chain("tenant-2", "owner-2", "camp-3", "landing-4") + [ind(L.tenant, "aio_flow", "flow-2")]),
        incident("r2", PLATFORM + chain("tenant-2", "owner-3", "camp-4", "landing-5") + [ind(L.tenant, "aio_flow", "flow-2")]),
    ])


def test_levels_and_level_graph():
    h = Hierarchy.from_dataset(hierarchy_ds())
    lv = {kind: h.level((layer, kind, value)) for layer, kind, value, _ in
          [ind(L.creative, "ad_caption", "caption one"), ind(L.tracking_account, "meta_pixel", "pixel-1"),
           ind(L.platform, "aio_sdk_asset", "sdk-const-1"), ind(L.tenant, "aio_flow", "flow-2"),
           ind(L.landing, "aio_landing", "landing-1")]}
    assert lv == {"ad_caption": Level.landing, "meta_pixel": Level.operator, "aio_sdk_asset": Level.platform,
                  "aio_flow": Level.tenant, "aio_landing": Level.landing}
    assert h.parents == {Level.landing: Level.campaign, Level.campaign: Level.operator,
                         Level.operator: Level.tenant, Level.tenant: None}
    assert h.summary()["level_graph"]["landing"] == "aio campaign"


def test_values_explained_by_a_shared_level_id_count_zero():
    ctx = ScoringContext.from_dataset(hierarchy_ds())
    s = score_pair(ctx, "a1/lander", "a3/lander", Relation.operator)
    (pixel,) = [r for r in s.rows if r.kind == "meta_pixel"]
    assert pixel.status is EvidenceStatus.hub_subsumed and pixel.bits == 0 and "owner" in pixel.note
    (owner,) = [r for r in s.rows if r.kind == "aio_owner"]
    assert owner.status is EvidenceStatus.shared and owner.bits > 0
    # a reseller's flow spanning two owners of one tenant is tenant-level, explained by the shared tenant
    s2 = score_pair(ctx, "r1/lander", "r2/lander", Relation.operator)
    (flow,) = [r for r in s2.rows if r.kind == "aio_flow"]
    assert flow.status is EvidenceStatus.hub_subsumed and "tenant" in flow.note


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
    assert cap.bits < HAND_TABLE.kinds["ad_caption"].bits - 2


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
