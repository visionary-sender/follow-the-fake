"""brand skin, platform-level and creative-only evidence never link into a campaign.

Placeholder values only, no case data.
"""

from __future__ import annotations

import json

import pytest

from ftf.contract.models import Capture, Dataset, Decision, Incident, Indicator, Layer, Level, Observed, Relation
from ftf.linking import ScoringContext, link_dataset, link_incident, score_pair
from ftf.linking import background as bg
from ftf.linking.background import digest
from ftf.linking.ledger import verdict

L = Layer


def ind(layer: Layer, kind: str, value: str, level: Level = Level.campaign, live: bool = False) -> tuple:
    return (layer, kind, value, level, live)


def incident(iid: str, inds: list[tuple]) -> Incident:
    cid = f"{iid}/lander"
    return Incident(
        id=iid, source="synthetic", captures=[Capture(id=cid, stage="lander")],
        indicators=[
            Indicator(id=f"{cid}/{kind}/{n}", capture_id=cid, layer=layer, kind=kind, level=level, value=value,
                      observed=Observed.live if live else Observed.static, extractor="synthetic@0")
            for n, (layer, kind, value, level, live) in enumerate(inds)
        ],
    )


PLATFORM = [
    ind(L.platform, "platform_name", "stack-a", Level.platform),
    ind(L.platform, "aio_sdk_asset", "sdk-const-1", Level.platform),
    ind(L.infrastructure, "third_party_domain", "cdn-a.example", Level.platform),
    ind(L.infrastructure, "third_party_host", "static.cdn-a.example", Level.platform),
]
BRAND = [
    ind(L.kit, "brand_asset", "outlet-logo"),
    ind(L.tracking_account, "google_analytics_id", "outlet-analytics-1", live=True),
]
CREATIVE = [
    ind(L.creative, "meta_ad_id", "ad-1"),
    ind(L.creative, "ad_caption", "Famous person reveals a secret"),
    ind(L.creative, "image_phash", "phash-1"),
]


def campaign_member(n: int) -> Incident:
    return incident(f"c{n}", PLATFORM + BRAND + CREATIVE + [
        ind(L.landing, "aio_landing", "landing-a"),
        ind(L.tracking_account, "meta_pixel", "pixel-a", live=True),
        ind(L.kit, "asset_sha256", f"asset-{n}"),
    ])


def fillers(k: int = 10) -> list[Incident]:
    return [incident(f"f{i}", [ind(L.landing, "aio_landing", f"landing-f{i}")]) for i in range(k)]


BRAND_LIST = frozenset({digest(L.tracking_account, "google_analytics_id", "outlet-analytics-1")})


@pytest.fixture()
def base(monkeypatch) -> Dataset:
    monkeypatch.setattr(bg, "default_brand", lambda: BRAND_LIST)
    monkeypatch.setattr("ftf.linking.ledger.default_brand", lambda: BRAND_LIST)
    monkeypatch.setattr("ftf.linking.index.default_brand", lambda: BRAND_LIST)
    return link_dataset(Dataset(name="gates", incidents=[campaign_member(1), campaign_member(2)] + fillers()))


def campaign_links(res, relations=(Relation.campaign, Relation.operator, Relation.template, Relation.tracking_account)):
    return [l for l in res.links if l.relation in relations]


def test_members_still_link(base):
    """Control: a substantive pair (landing id and live pixel) still makes one campaign."""
    camps = [c for c in base.campaigns if c.relation is Relation.campaign and len(c.capture_ids) > 1]
    assert [sorted(c.capture_ids) for c in camps] == [["c1/lander", "c2/lander"]]


def test_brand_assets_only_no_link_above_ecosystem(base):
    res = link_incident(base, incident("real", BRAND))
    assert res.assignment.decision is not Decision.assign
    assert campaign_links(res) == []
    ds = Dataset(name="pair", incidents=[campaign_member(1), incident("real", BRAND)] + fillers())
    ctx = ScoringContext.from_dataset(ds, brand=BRAND_LIST)
    for rel in (Relation.campaign, Relation.operator, Relation.template, Relation.tracking_account):
        s = score_pair(ctx, "c1/lander", "real/lander", rel)
        assert s.bits <= 0, rel
    eco = score_pair(ctx, "c1/lander", "real/lander", Relation.ecosystem)
    assert eco.bits > 0  # brand skin still counts at ecosystem level


def test_brand_stoplist_value_is_hub_under_campaign_only():
    ds = Dataset(name="pair", incidents=[incident("a", BRAND), incident("b", BRAND)] + fillers())
    on = ScoringContext.from_dataset(ds, brand=BRAND_LIST)
    off = ScoringContext.from_dataset(ds, brand=frozenset())
    row = next(r for r in score_pair(on, "a/lander", "b/lander").rows if r.kind == "google_analytics_id")
    assert row.status.value == "hub_subsumed" and row.bits == 0 and "brand skin" in row.note
    assert score_pair(off, "a/lander", "b/lander").bits > 0  # without the stop-list: as before


def test_platform_only_page_no_link(base):
    res = link_incident(base, incident("plat", PLATFORM))
    assert res.assignment.decision is not Decision.assign
    assert campaign_links(res) == []
    assert all(l.relation is Relation.ecosystem for l in res.links)


def test_platform_and_creative_never_link_alone():
    """Platform constants plus a shared creative: two positive layers, but nothing substantive."""
    ds = Dataset(name="pc", incidents=[incident("a", PLATFORM + CREATIVE), incident("b", PLATFORM + CREATIVE)]
                 + fillers())
    ctx = ScoringContext.from_dataset(ds, brand=frozenset())
    s = score_pair(ctx, "a/lander", "b/lander")
    assert not s.substantive and s.layers_positive <= 1 and not s.live_private
    assert verdict(s, ctx) != "link"


def test_creative_only_incident_not_assigned(base):
    res = link_incident(base, incident("celeb", CREATIVE))
    assert res.assignment.decision is not Decision.assign
    assert campaign_links(res) == []


def test_indexed_and_linear_paths_agree(base):
    from ftf.linking.backend import Backend

    for inc in (incident("plat", PLATFORM), incident("celeb", CREATIVE), incident("real", BRAND)):
        a = link_incident(base, inc)
        b = link_incident(base, inc, backend=Backend.from_dataset)
        assert a.assignment.decision == b.assignment.decision
        assert sorted((l.id, l.relation) for l in a.links) == sorted((l.id, l.relation) for l in b.links)


def test_cached_index_dropped_when_stoplist_changes(base, monkeypatch):
    from ftf.linking.index import CandidateIndex, cached_index

    CandidateIndex.build(base)  # cached for this very object
    assert cached_index(base) is not None
    monkeypatch.setattr("ftf.linking.index.default_brand", lambda: frozenset())
    assert cached_index(base) is None


def test_stoplist_loading(tmp_path, monkeypatch, capsys):
    path = tmp_path / "brand-stoplist.json"
    path.write_text(json.dumps(bg.brand_stoplist_doc({"d1", "d2"}, 2)))
    monkeypatch.setattr(bg, "_LOADED", {})
    monkeypatch.setattr(bg, "_WARNED", set())
    monkeypatch.setenv(bg.BRAND_STOPLIST_ENV, str(path))
    assert bg.default_brand() == frozenset({"d1", "d2"})
    monkeypatch.setenv(bg.BRAND_STOPLIST_ENV, "off")
    assert bg.default_brand() == frozenset()
    monkeypatch.setenv(bg.BRAND_STOPLIST_ENV, str(tmp_path / "missing.json"))
    assert bg.default_brand() == frozenset()
    assert "brand stop-list not loaded" in capsys.readouterr().err
    monkeypatch.delenv(bg.BRAND_STOPLIST_ENV)
    assert bg.stoplist_path() == bg.DEFAULT_BRAND_STOPLIST


def test_brand_stoplist_command(tmp_path):
    from ftf.cli import main

    real = incident("real-1", BRAND)
    corpus = Dataset(name="corpus", incidents=[real, incident("scam-1", PLATFORM)])
    cpath = tmp_path / "dataset.json"
    cpath.write_text(corpus.model_dump_json())
    (tmp_path / "corpus_kind.jsonl").write_text(
        json.dumps({"incident_id": "real-1", "corpus_kind": "must_not_link"}) + "\n"
        + json.dumps({"incident_id": "scam-1", "corpus_kind": "fetched_landing"}) + "\n")
    out = tmp_path / "out.json"
    assert main(["brand-stoplist", str(cpath), "-o", str(out)]) == 0
    got = bg.read_brand_stoplist(out)
    assert digest(L.tracking_account, "google_analytics_id", "outlet-analytics-1") in got
    assert digest(L.platform, "aio_sdk_asset", "sdk-const-1") not in got
    assert "outlet-analytics-1" not in out.read_text()


@pytest.mark.parametrize("kind", ["brand", "person_face", "brand_asset"])
def test_visual_brand_and_face_kinds_are_ecosystem_only(kind):
    from ftf.linking.weights import HAND_TABLE

    for rel in (Relation.campaign, Relation.operator, Relation.template, Relation.tracking_account):
        assert HAND_TABLE.scale(kind, HAND_TABLE.kinds[kind].layer, rel) == 0, rel
    assert HAND_TABLE.scale(kind, HAND_TABLE.kinds[kind].layer, Relation.ecosystem) > 0
