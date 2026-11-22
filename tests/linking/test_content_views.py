"""phase 2: calibrated content views wired into the ledger and assignment. Synthetic data only."""

from __future__ import annotations

import json

import numpy as np
import pytest

from ftf import cli
from ftf.contract.models import Dataset, Decision, Incident, Indicator, Layer, Relation, VectorRef
from ftf.features.common import VectorStore as VectorWriter
from ftf.linking.assign import combine, link_incident
from ftf.linking.grouping import link_dataset
from ftf.linking.index import CandidateIndex
from ftf.linking.grouping import qualifies
from ftf.linking.ledger import ScoringContext, is_content_row, score_pair, threshold, verdict, with_content
from ftf.linking.similarity import FACE_CAP_BITS, Calibration, ContentViews, DatasetVectorStore, content_views, save_calibrations

from linking_support import cap, dataset, incident

L = Layer
MODEL = "test/model-a"
STORE = VectorWriter(".", MODEL).store
DIM = 8
SIG = " ".join(str(i) for i in range(16))  # a MinHash signature


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def rng(seed=0):
    return np.random.default_rng(seed)


def vec_ind(capture, kind="image_embedding", layer="creative"):
    iid = f"{capture}/{kind}/0"
    return Indicator(id=iid, capture_id=capture, layer=layer, kind=kind, level="campaign",
                     vector_ref=VectorRef(store=STORE, key=iid, model=MODEL, dim=DIM),
                     observed="static", extractor="test@0")


def val_ind(capture, value, kind="dom_minhash", layer="landing"):
    return Indicator(id=f"{capture}/{kind}/0", capture_id=capture, layer=layer, kind=kind, level="campaign",
                     value=value, observed="static", extractor="test@0")


def with_inds(inc: Incident, extra: list[Indicator]) -> Incident:
    data = inc.model_dump(mode="json")
    data["indicators"] += [i.model_dump(mode="json") for i in extra]
    return Incident.model_validate(data)


def _inc(iid, tenant, owner, landing, pixel):
    caps = [cap(f"{iid}-ad", "ad", arts=("ad_screenshot",)), cap(f"{iid}-l", "lander")]
    vals = {
        f"{iid}-ad": [(L.creative, f"caption-{iid}")],
        f"{iid}-l": [(L.tenant, tenant, True), (L.owner, owner), (L.landing, landing, True), (L.tracking_account, pixel, True), (L.platform, "platform-x")],
    }
    return incident(iid, caps, vals)


A = unit([1, 0, 0, 0, 0, 0, 0, 0])  # creative of campaign a
B = unit([0, 1, 0, 0, 0, 0, 0, 0])  # creative of campaign b


def base_incidents():
    out = []
    for i in range(3):
        out.append(with_inds(_inc(f"a{i}", "tenant-a", "owner-a", "landing-a", "pixel-a"), [vec_ind(f"a{i}-ad"), val_ind(f"a{i}-ad", SIG)]))
    for i in range(3):
        out.append(with_inds(_inc(f"b{i}", "tenant-b", "owner-b", "landing-b", "pixel-b"), [vec_ind(f"b{i}-ad")]))
    for i in range(4):
        out.append(with_inds(_inc(f"z{i}", f"tenant-z{i}", f"owner-z{i}", f"landing-z{i}", f"pixel-z{i}"), [vec_ind(f"z{i}-ad")]))
    return out


def write_vectors(directory, vectors):
    w = VectorWriter(directory, MODEL)
    for k, v in vectors.items():
        w.add(k, np.asarray(v, dtype=np.float32))
    w.save()


def base_vectors():
    r = rng(3)
    vecs = {}
    for i in range(3):
        vecs[f"a{i}-ad/image_embedding/0"] = unit(A + 0.02 * r.normal(size=DIM))
        vecs[f"b{i}-ad/image_embedding/0"] = unit(B + 0.02 * r.normal(size=DIM))
    for i in range(4):
        vecs[f"z{i}-ad/image_embedding/0"] = unit(r.normal(size=DIM))
    return vecs


def calibrations(cap_bits=4.0):
    null = np.clip(rng(5).normal(0.0, 0.3, 2000), -1, 1)
    pos = np.clip(rng(6).normal(0.97, 0.01, 50), -1, 1)
    cals = {k: Calibration.fit(null, pos, cap_bits, view=k) for k in ("image_embedding", "text_embedding", "face_embedding")}
    mh_null = np.clip(rng(7).normal(0.1, 0.05, 2000), 0, 1)
    mh_pos = np.clip(rng(8).normal(0.95, 0.03, 50), 0, 1)
    cals["dom_minhash"] = Calibration.fit(mh_null, mh_pos, cap_bits, view="dom_minhash")
    return cals


@pytest.fixture
def world(tmp_path):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    write_vectors(base_dir, base_vectors())
    ds = dataset(*base_incidents())
    (base_dir / "dataset.json").write_text(ds.to_json())
    linked = link_dataset(ds)  # content views off: grouped on identifiers
    return ds, linked, base_dir


def screenshot_item(tmp_path, vector, with_minhash=False):
    """A screenshot-only item: one ad capture, no identifier, only content."""
    new_dir = tmp_path / "new"
    new_dir.mkdir(exist_ok=True)
    write_vectors(new_dir, {"s-ad/image_embedding/0": vector})
    c = cap("s-ad", "ad", arts=("ad_screenshot",))
    inds = [vec_ind("s-ad")] + ([val_ind("s-ad", SIG)] if with_minhash else [])
    return Incident(id="s", source="synthetic", captures=[c], indicators=inds), new_dir


def all_pairs(ctx):
    caps = sorted(ctx.values)
    return [(a, b) for i, a in enumerate(caps) for b in caps[i + 1 :]]


# --------------------------------------------------------------------------- off switch


def test_off_switch_scores_identical(world):
    ds, _, base_dir = world
    off = ScoringContext.from_dataset(ds)
    # every way of "on but nothing usable" is off: no calibrations, vector views without a store
    assert content_views({}, DatasetVectorStore(base_dir)) is None
    only_vec = {k: v for k, v in calibrations().items() if k != "dom_minhash"}
    assert content_views(only_vec, None) is None
    assert with_content(off, None) is off
    # on, with a calibration for a view the data does not carry: the scoring path runs, nothing changes
    idle = ScoringContext.from_dataset(ds, content=ContentViews({"text_embedding": calibrations()["text_embedding"]}, DatasetVectorStore(base_dir)))
    assert idle.content is not None
    for rel in Relation:
        for a, b in all_pairs(off):
            assert score_pair(idle, a, b, rel) == score_pair(off, a, b, rel)
    from ftf.linking.backend import Backend

    assert link_dataset(ds, backend=Backend.from_context(idle)) == link_dataset(ds)


def test_off_switch_assignment_identical(world, tmp_path):
    _, linked, base_dir = world
    item, new_dir = screenshot_item(tmp_path, A)
    plain = link_incident(linked, item)
    off = link_incident(linked, item, base_dir=base_dir, incident_dir=new_dir, calibrations=None)
    assert off.assignment == plain.assignment and off.links == plain.links


# --------------------------------------------------------------------------- caps and layers


def test_content_rows_capped_once_per_layer_and_never_positive(world):
    ds, _, base_dir = world
    cals = calibrations(cap_bits=4.0)
    on = ScoringContext.from_dataset(ds, content=ContentViews(cals, DatasetVectorStore(base_dir)))
    off = ScoringContext.from_dataset(ds)
    for rel in Relation:
        for a, b in all_pairs(off):
            ps_on, ps_off = score_pair(on, a, b, rel), score_pair(off, a, b, rel)
            # content never makes a layer positive, opens the campaign gate or a strict link
            assert (ps_on.layers_positive, ps_on.live_private) == (ps_off.layers_positive, ps_off.live_private)
            assert ps_on.bits >= ps_off.bits - 1e-9
            content = [r for r in ps_on.rows if is_content_row(r)]
            for r in content:
                assert 0 <= r.bits <= 4.0 + 1e-9
            # one counted term per layer: creative never exceeds the cap however many views agree
            creative = sum(r.bits for r in ps_on.rows if r.layer is L.creative and r.bits > 0)
            assert creative <= max(4.0, sum(r.bits for r in ps_off.rows if r.layer is L.creative and r.bits > 0)) + 1e-9
    ps = score_pair(on, "a0-ad", "a1-ad", Relation.campaign)
    img = next(r for r in ps.rows if r.kind == "image_embedding")
    assert img.bits == pytest.approx(4.0)  # near-identical creatives: at the cap
    assert "content view" in img.note
    # operator: the creative layer's 0.5 scale, halved again
    op = next(r for r in score_pair(on, "a0-ad", "a1-ad", Relation.operator).rows if r.kind == "image_embedding")
    assert op.bits == pytest.approx(1.0)
    # tracking_account: the creative layer says nothing about it
    assert not any(is_content_row(r) for r in score_pair(on, "a0-ad", "a1-ad", Relation.tracking_account).rows)


def test_two_views_in_one_layer_count_once(tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    r = rng(1)
    vecs = {}
    incs = []
    for name in ("p", "q"):
        c = f"{name}-ad"
        inds = [vec_ind(c, "image_embedding"), vec_ind(c, "text_embedding")]
        vecs[f"{c}/image_embedding/0"] = unit(A + 0.01 * r.normal(size=DIM))
        vecs[f"{c}/text_embedding/0"] = unit(B + 0.01 * r.normal(size=DIM))
        incs.append(Incident(id=name, source="synthetic", captures=[cap(c, "ad", arts=("ad_screenshot",))], indicators=inds))
    write_vectors(d, vecs)
    ctx = ScoringContext.from_dataset(dataset(*incs), content=ContentViews(calibrations(), DatasetVectorStore(d)))
    ps = score_pair(ctx, "p-ad", "q-ad", Relation.campaign)
    content = [r for r in ps.rows if is_content_row(r)]
    assert sorted(r.kind for r in content) == ["image_embedding", "text_embedding"]
    assert sorted(r.bits for r in content)[0] == 0.0  # the second view is subsumed by the within-layer max
    assert ps.bits == pytest.approx(4.0)
    assert ps.layers_positive == 0


def test_face_view_low_cap_unless_null_holds_same_face(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    write_vectors(d, {"p-ad/face_embedding/0": A, "q-ad/face_embedding/0": A})
    incs = [Incident(id=n, source="synthetic", captures=[cap(f"{n}-ad", "ad", arts=("ad_screenshot",))], indicators=[vec_ind(f"{n}-ad", "face_embedding")]) for n in ("p", "q")]
    ds = dataset(*incs)
    cals = calibrations()
    low = score_pair(ScoringContext.from_dataset(ds, content=ContentViews(cals, DatasetVectorStore(d))), "p-ad", "q-ad")
    assert low.bits == pytest.approx(FACE_CAP_BITS)
    assert "same celebrity" in next(r.note for r in low.rows if r.kind == "face_embedding")
    cals["face_embedding"].null_same_face = True
    full = score_pair(ScoringContext.from_dataset(ds, content=ContentViews(cals, DatasetVectorStore(d))), "p-ad", "q-ad")
    assert full.bits == pytest.approx(4.0)


# --------------------------------------------------------------------------- assignment


def test_screenshot_only_item_reaches_review_with_pointers_never_assign(world, tmp_path):
    _, linked, base_dir = world
    item, new_dir = screenshot_item(tmp_path, unit(A + 0.01 * rng(9).normal(size=DIM)))
    # a high cap, so content alone clears the assign bar in bits: it must still not assign
    cals = calibrations(cap_bits=12.0)
    cals["image_embedding"].slope *= 4  # a very strong view (bounded by the null tail, ~11 bits)
    res = link_incident(linked, item, base_dir=base_dir, incident_dir=new_dir, calibrations=cals)
    a = res.assignment
    assert a.decision is Decision.review
    top = a.candidates[0]
    camp = next(c for c in linked.campaigns if c.id == top.campaign_id)
    assert "a0-ad" in camp.capture_ids
    assert "similar content: " in top.note and "image_embedding cosine" in top.note
    k = sum(1 for c in linked.campaigns if c.relation is Relation.campaign)
    tau = threshold(ScoringContext.from_dataset(combine(linked, [item])), Relation.campaign)
    assert top.score_bits >= tau + np.log2(k)  # content alone clears the bar in bits ...
    assert all(l.level.value == "loose" for l in res.links)  # ... but makes no qualifying link
    # without content views the same item gets no pointer
    plain = link_incident(linked, item).assignment
    assert plain.decision is Decision.review
    assert all("similar content" not in (c.note or "") for c in plain.candidates)


def test_indexed_equals_linear_with_views_on(world, tmp_path):
    _, linked, base_dir = world
    cals = calibrations()
    for vector in (A, B, unit(rng(11).normal(size=DIM))):
        item, new_dir = screenshot_item(tmp_path, vector, with_minhash=False)
        for extra in ([], [val_ind("s-ad", "caption-a0", kind="creative_id", layer="creative")]):
            inc = with_inds(item, extra)
            linear = link_incident(linked, inc, base_dir=base_dir, incident_dir=new_dir, calibrations=cals)
            idx = CandidateIndex.build(linked, cache=False)
            indexed = link_incident(linked, inc, base_dir=base_dir, incident_dir=new_dir, calibrations=cals, index=idx)
            assert indexed.assignment == linear.assignment
            assert indexed.links == linear.links
    # a lander that matches campaign a on identifiers is still assigned, with or without views
    lander = _inc("n", "tenant-a", "owner-a", "landing-a", "pixel-a")
    on = link_incident(linked, lander, base_dir=base_dir, calibrations=cals, index=CandidateIndex.build(linked, cache=False))
    off = link_incident(linked, lander, index=CandidateIndex.build(linked, cache=False))
    assert on.assignment.decision is off.assignment.decision is Decision.assign
    assert on.assignment.campaign_id == off.assignment.campaign_id


# --------------------------------------------------------------------------- CLI


def test_cli_calibrate_then_assign(world, tmp_path):
    ds, linked, base_dir = world
    out = tmp_path / "cal" / "calibration.json"
    assert cli.main(["calibrate-similarity", str(base_dir / "dataset.json"), "-o", str(out), "--max-null", "1000"]) == 0
    doc = json.loads(out.read_text())
    assert set(doc["views"]) >= {"image_embedding"}
    report = json.loads((tmp_path / "cal" / "calibration.quality.json").read_text())
    q = report["views"]["image_embedding"]
    assert q["n_pos"] > 0 and q["n_null"] > 0 and {"auc", "cllr", "cllr_min"} <= set(q) and set(q["sim_at_bits"]) == {"1", "2", "4"}

    (base_dir / "linked.json").write_text(linked.to_json())
    item, new_dir = screenshot_item(tmp_path, A, with_minhash=False)
    (new_dir / "new.json").write_text(dataset(item).to_json())
    save_calibrations(calibrations(), tmp_path / "cal2.json")
    res = tmp_path / "out.json"
    assert cli.main(["assign", str(base_dir / "linked.json"), str(new_dir / "new.json"), "-o", str(res),
                     "--content-views", str(tmp_path / "cal2.json")]) == 0
    a = Dataset.from_json(res.read_bytes()).assignments[0]
    assert a.decision is Decision.review and "similar content" in a.candidates[0].note
    # score and link take the switch too
    assert cli.main(["score", str(base_dir / "dataset.json"), "a0-ad", "a1-ad", "--content-views", str(tmp_path / "cal2.json")]) == 0
    assert cli.main(["link", str(base_dir / "dataset.json"), "-o", str(tmp_path / "l.json"), "--content-views", str(tmp_path / "cal2.json")]) == 0


def test_content_never_lifts_a_pair_over_tau(tmp_path):
    """Three identifier layers just under tau plus a near-identical creative: review, never link or assign."""
    d = tmp_path / "u"
    d.mkdir()
    r = rng(2)
    incs, vecs = [], {}
    for n in ("p", "q"):
        c = f"{n}-l"
        inc = incident(n, [cap(c, "lander")], {c: [(L.landing, "landing-pq"), (L.infrastructure, "host-pq"), (L.offer, "offer-pq")]})
        incs.append(with_inds(inc, [vec_ind(c)]))
        vecs[f"{c}/image_embedding/0"] = unit(A + 0.01 * r.normal(size=DIM))
    for i in range(46):  # unrelated captures raise tau above the pair's identifier bits
        c = f"f{i}-l"
        incs.append(with_inds(incident(f"f{i}", [cap(c, "lander")], {c: [(L.landing, f"landing-f{i}")]}), [vec_ind(c)]))
        vecs[f"{c}/image_embedding/0"] = unit(r.normal(size=DIM))
    write_vectors(d, vecs)
    ds = dataset(*incs)
    off = ScoringContext.from_dataset(ds)
    on = ScoringContext.from_dataset(ds, content=ContentViews(calibrations(), DatasetVectorStore(d)))
    tau = threshold(off, Relation.campaign)
    ps_off, ps_on = score_pair(off, "p-l", "q-l"), score_pair(on, "p-l", "q-l")
    assert ps_off.bits < tau <= ps_on.bits and ps_on.layers_positive >= 2
    assert ps_on.id_bits == ps_off.bits
    assert not qualifies(ps_on, tau) and verdict(ps_on, on) == "review"
    # the partition is the same with content views on, for every relation
    from ftf.linking.backend import Backend

    parts = lambda x: sorted((c.relation.value, sorted(c.capture_ids)) for c in x.campaigns)
    linked_on = link_dataset(ds, backend=Backend.from_context(on))
    assert parts(linked_on) == parts(link_dataset(ds))
    # assigning a third copy of the pair: never assign on the content bits
    c = "n-l"
    new = with_inds(incident("n", [cap(c, "lander")], {c: [(L.landing, "landing-pq"), (L.infrastructure, "host-pq"), (L.offer, "offer-pq")]}), [vec_ind(c)])
    nd = tmp_path / "n"
    nd.mkdir()
    write_vectors(nd, {"n-l/image_embedding/0": unit(A)})
    plain = link_incident(link_dataset(ds), new).assignment
    res = link_incident(link_dataset(ds), new, base_dir=d, incident_dir=nd, calibrations=calibrations()).assignment
    assert plain.decision is Decision.new
    assert res.decision is Decision.review  # similar content lifts new to review, never to assign


def test_world_partitions_equal_on_and_off(world):
    ds, linked, base_dir = world
    from ftf.linking.backend import Backend

    on = ScoringContext.from_dataset(ds, content=ContentViews(calibrations(), DatasetVectorStore(base_dir)))
    parts = lambda x: sorted((c.relation.value, sorted(c.capture_ids)) for c in x.campaigns)
    assert parts(link_dataset(ds, backend=Backend.from_context(on))) == parts(linked)
