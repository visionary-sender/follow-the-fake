import pytest

from ftf.app import relations as R
from ftf.app.figures import edge_hover, indicator_figure, network_figure
from ftf.contract import EvidenceStatus, Layer, LinkLevel, Relation, linkable
from ftf.contract.fixture import fixture_dir, load_fixture

from .synthetic import HOSTILE, dataset


@pytest.fixture(scope="module")
def ds():
    return dataset()


def test_tracking_graph_has_pixel_edge_and_ignores_collector(ds):
    g = R.relation_graph(ds, Relation.tracking_account)
    assert set(g.nodes) == {"i1/lander", "i2/lander"}
    (e,) = g["i1/lander"]["i2/lander"]["edges"]
    assert [s.kind for s in e.shared] == ["meta_pixel"]
    assert [r.kind for r in e.excluded] == ["aio_fingerprint"]
    assert e.tier == "Strong"
    assert g.nodes["i1/lander"]["campaign_ids"] == ("C-px",)


def test_collector_only_link_never_draws(ds):
    g = R.relation_graph(ds, Relation.operator)
    assert g.number_of_edges() == 0
    for rel in R.RELATIONS:
        for e in R.edges(ds, rel):
            assert all(s.layer != Layer.collector.value for s in e.shared)


def test_clusters_campaign_then_unlabelled_component(ds):
    cl = R.clusters(ds, Relation.template)
    assert [c.source for c in cl] == ["campaign", "component"]
    assert cl[0].id == "C-kit"
    assert set(cl[0].link_ids) == {"L-k23", "L-k34"}  # derived: campaign lists no links
    assert set(cl[1].capture_ids) == {"i1/ad", "i3/ad"}


def test_campaign_summary(ds):
    (s,) = R.campaign_summary(ds, Relation.tracking_account)
    assert (s.label, s.n_captures, s.n_incidents, s.n_links) == ("Pixel campaign", 2, 2, 1)
    assert s.top_indicators[0].kind == "meta_pixel"
    assert (s.first_seen, s.last_seen) == ("2026-10-01", "2026-10-05")
    (k,) = R.campaign_summary(ds, Relation.template)
    assert k.top_indicators[0].links == 2 and k.top_indicators[0].max_bits == 5.0
    assert len(R.campaign_summary(ds)) == 2


def test_indicator_view_skips_collector(ds):
    g = R.indicator_view(ds, "C-kit")
    inds = [d for _, d in g.nodes(data=True) if d["side"] == "indicator"]
    assert [(d["kind"], d["n"]) for d in inds] == [("kit_hash", 3)]
    assert all(d["layer"] != "collector" for d in inds)
    with pytest.raises(KeyError):
        R.indicator_view(ds, "nope")


def test_explain_pair_direct_and_path(ds):
    (d,) = R.explain_pair(ds, "i1/lander", "i2/lander")
    assert d.relation is Relation.tracking_account and d.direct and d.campaign_ids == ("C-px",)
    (p,) = R.explain_pair(ds, "i2/lander", "i4/lander")
    assert p.relation is Relation.template and not p.direct
    assert p.path == ("i2/lander", "i3/lander", "i4/lander")
    assert [e.link_id for e in p.edges] == ["L-k23", "L-k34"]


def test_explain_pair_collector_is_no_reason(ds):
    assert R.explain_pair(ds, "i1/lander", "i4/lander") == []
    assert R.explain_pair(ds, "i1/lander", "i1/lander") == []
    with pytest.raises(KeyError):
        R.explain_pair(ds, "i1/lander", "missing")


def test_tier_and_threshold(ds):
    t = R.review_threshold_bits(ds)  # 8 captures -> 3 bits
    assert t == pytest.approx(3.0)
    assert R.tier(LinkLevel.strict, 9.0, t) == "Strong"
    assert R.tier(LinkLevel.loose, 1.0, t) == R.INSUFFICIENT
    (e,) = [e for e in R.edges(ds, Relation.template) if e.link_id == "L-k34"]
    assert e.tier == R.INSUFFICIENT


def test_evidence_rows_label_missing_and_collector(ds):
    (e,) = [e for e in R.edges(ds, Relation.template) if e.link_id == "L-k34"]
    rows = R.evidence_rows(e)
    assert rows[0]["evidence"] == "shared"
    assert rows[1]["note"] == R.MISSING_NOTE
    (px,) = R.edges(ds, Relation.tracking_account)
    assert R.evidence_rows(px)[-1]["note"] == R.COLLECTOR_NOTE


def test_defang():
    assert R.defang("https://scam.example.com/a") == "hxxps://scam[.]example[.]com/a"
    assert R.defang("px-1") == "px-1"
    assert R.defang(None) == ""


def test_figures_escape_hostile_values(ds):
    (e,) = [e for e in R.edges(ds, Relation.template) if e.link_id == "L-ad"]
    hover = edge_hover(e)
    assert "<a" not in hover and "http://" not in hover and "&lt;a" in hover
    fig = network_figure(R.relation_graph(ds, Relation.template), {"i2/lander"})
    texts = [str(t.hovertext) for t in fig.data if t.hovertext is not None]
    assert texts and not any(HOSTILE in t or "<a " in t for t in texts)
    assert indicator_figure(R.indicator_view(ds, "C-kit")).data
    assert network_figure(R.relation_graph(ds, Relation.operator)).layout.annotations


_has_fixture = (fixture_dir() / "dataset.json").is_file()


@pytest.mark.skipif(not _has_fixture, reason="fixture not present")
def test_fixture_tracking_account_link_and_no_collector_edges():
    fx = load_fixture()
    ok = {i.id for i in fx.indicators() if linkable(i)}
    for rel in R.RELATIONS:
        for e in R.edges(fx, rel):
            assert all(s.layer != Layer.collector.value for s in e.shared)
    # A shared tracking-account indicator (the Meta pixel) joins two cases; the fixture files
    # that link under whichever relation the linker chose, so only the evidence layer is fixed.
    tracked = [
        l for l in fx.links if any(
            r.layer is Layer.tracking_account and r.status is EvidenceStatus.shared
            and all(i in ok for i in r.indicator_ids) for r in l.evidence)
    ]
    assert tracked
    l = tracked[0]
    assert fx.incident_of(l.a).id != fx.incident_of(l.b).id
    answers = R.explain_pair(fx, l.a, l.b)
    ans = next(a for a in answers if a.relation is l.relation)
    assert ans.direct
    assert any(s.layer == Layer.tracking_account.value for e in ans.edges for s in e.shared)
    for c in fx.campaigns:
        R.indicator_view(fx, c.id)
    assert len(R.campaign_summary(fx)) == len(fx.campaigns)
