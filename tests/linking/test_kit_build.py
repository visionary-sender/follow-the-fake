"""a corroborated landing build counts for the campaign; a copied kit alone stays template-level.

Placeholder values only, no case data.
"""

from __future__ import annotations

from ftf.contract.models import Capture, Dataset, Incident, Indicator, Layer, Level, Observed, Relation
from ftf.linking import ScoringContext, score_pair
from ftf.linking.ledger import kit_build

L = Layer


def incident(iid: str, inds: list[tuple[Layer, str, str]]) -> Incident:
    cid = f"{iid}/lander"
    return Incident(
        id=iid, source="synthetic", captures=[Capture(id=cid, stage="lander")],
        indicators=[
            Indicator(id=f"{cid}/{kind}/{n}", capture_id=cid, layer=layer, kind=kind, level=Level.campaign,
                      value=value, observed=Observed.static, extractor="synthetic@0")
            for n, (layer, kind, value) in enumerate(inds)
        ],
    )


SKELETON = (L.kit, "dom_skeleton_sha256", "skel-1")
SCRIPT = (L.kit, "inline_script_sha256", "script-1")
PHASH = (L.creative, "image_phash", "phash-1")


def ctx_for(a: list, b: list, extra: list[Incident] = ()) -> ScoringContext:
    fillers = [incident(f"f{i}", [(L.landing, "aio_landing", f"landing-f{i}")]) for i in range(8)]
    return ScoringContext.from_dataset(Dataset(name="kit", incidents=[incident("a", a), incident("b", b),
                                                                      *extra, *fillers]))


def build_of(ctx: ScoringContext, relation: Relation = Relation.campaign) -> bool:
    return kit_build(ctx, ctx.values["a/lander"], ctx.values["b/lander"], relation)


def kit_rows(ctx: ScoringContext):
    ps = score_pair(ctx, "a/lander", "b/lander", Relation.campaign)
    return ps, {r.kind: r.bits for r in ps.rows if r.layer is L.kit and r.bits > 0}


def test_skeleton_with_creative_is_a_campaign_build():
    ctx = ctx_for([SKELETON, SCRIPT, PHASH], [SKELETON, SCRIPT, PHASH])
    assert build_of(ctx)
    ps, kit = kit_rows(ctx)
    # full scale (3.5 and 3.0 bits) minus log2(2); page and script are one term each
    assert kit == {"dom_skeleton_sha256": 2.5, "inline_script_sha256": 2.0}
    assert ps.layers_positive >= 2


def test_structure_without_creative_is_no_build():
    # one operator reuses its build across campaigns: two rare structure kinds alone stay template-level
    assert not build_of(ctx_for([SKELETON, SCRIPT, (L.kit, "asset_norm_sha256", "n-1")],
                                [SKELETON, SCRIPT, (L.kit, "asset_norm_sha256", "n-1")]))


def test_copied_kit_alone_stays_template_level():
    ctx = ctx_for([SKELETON], [SKELETON])
    assert not build_of(ctx)
    ps, kit = kit_rows(ctx)
    assert kit == {"dom_skeleton_sha256": 0.75}  # kit campaign scale 0.5
    assert ps.layers_positive <= 1  # campaign gate closed


def test_creative_alone_is_no_build():
    assert not build_of(ctx_for([PHASH, (L.kit, "asset_norm_sha256", "n-1")], [PHASH, (L.kit, "asset_norm_sha256", "n-1")]))


def test_common_skeleton_is_no_build():
    third = incident("c", [SKELETON])
    assert not build_of(ctx_for([SKELETON, PHASH], [SKELETON, PHASH], [third]))


def test_operator_relation_unchanged():
    assert not build_of(ctx_for([SKELETON, PHASH], [SKELETON, PHASH]), Relation.operator)


def test_only_the_rare_value_gets_campaign_scale():
    # the skeleton is rare and backed by the creative; the shared script is common (3 pre-groups) and keeps
    # its template scale (3.0 × 0.5 − log2(3) < hub_bits)
    others = [incident("c", [SCRIPT]), incident("d", [SCRIPT])]
    ctx = ctx_for([SKELETON, SCRIPT, PHASH], [SKELETON, SCRIPT, PHASH], others)
    assert {v[1] for v in build_of(ctx)} == {"dom_skeleton_sha256"}
    _, kit = kit_rows(ctx)
    assert kit == {"dom_skeleton_sha256": 2.5}


def test_masked_text_template_is_no_corroboration():
    tmpl = (L.creative, "text_template", "tmpl-1")
    assert not build_of(ctx_for([SKELETON, tmpl], [SKELETON, tmpl]))
