"""Candidate index: same results as the linear scan, no re-clustering, JSON round trip."""

from __future__ import annotations

import dataclasses
import sys

import numpy as np
import pytest

from ftf import cli
from ftf.contract.models import Dataset, Decision, Layer, Relation
from ftf.linking import assign, grouping, ledger, link_dataset, link_incident
from ftf.linking.assign import combine
from ftf.linking.index import CandidateIndex, cached_index, clear_cache, extend_context
from ftf.linking.ledger import ScoringContext

import synthetic_corpus as sc
from linking_support import simple, stub_backend

L = Layer


def _split(n: int = 60, seed: int = 7, cut: float = 0.7) -> tuple[Dataset, list]:
    incs = sc.corpus(n, seed=seed)
    k = int(len(incs) * cut)
    base = link_dataset(sc.dataset(incs[:k]))
    known = {c.id for i in incs[:k] for c in i.captures}
    # single-incident tests: only new incidents whose duplicates point into the base
    new = [i for i in incs[k:] if all(c.duplicate_of is None or c.duplicate_of in known for c in i.captures)]
    return base, new


def _plain(x):
    """Comparable plain form of a context field: mappings and sets recursively, _Values as tuples."""
    from collections.abc import Mapping, Set

    from ftf.linking.ledger import _Values

    if isinstance(x, _Values):
        return (_plain(x.ids), _plain(x.display), _plain(x.live))
    if isinstance(x, Mapping):
        return {k: _plain(x[k]) for k in set(x)}
    if isinstance(x, (Set, list, tuple)) and not isinstance(x, str):
        return type(x)(_plain(v) for v in x) if not isinstance(x, Set) else frozenset(_plain(v) for v in x)
    return x


def _ctx_view(ctx: ScoringContext) -> dict:
    """Every field of the context (new ones included), except the dataset object itself."""
    return {f.name: _plain(getattr(ctx, f.name)) for f in dataclasses.fields(ctx) if f.name not in ("ds", "weights")}


@pytest.mark.parametrize("seed", [7, 8, 9])
def test_extended_context_equals_a_rebuild(seed):
    base, new = _split(seed=seed)
    idx = CandidateIndex.build(base, cache=False)
    for inc in new:
        ext = idx.extend(inc)
        full = ScoringContext.from_dataset(combine(base, [inc]))
        assert _ctx_view(ext) == _ctx_view(full), inc.id


@pytest.mark.parametrize("relation", list(Relation))
@pytest.mark.parametrize("seed", [7, 8])
def test_index_equals_linear_scan_with_the_real_ledger(seed, relation):
    incs = sc.corpus(60, seed=seed)
    k = 42
    base = link_dataset(sc.dataset(incs[:k]), [relation])
    known = {c.id for i in incs[:k] for c in i.captures}
    new = [i for i in incs[k:] if all(c.duplicate_of is None or c.duplicate_of in known for c in i.captures)]
    idx = CandidateIndex.build(base, cache=False)
    decisions = set()
    for inc in new:
        lin = link_incident(base, inc, relation)
        fast = link_incident(base, inc, relation, index=idx)
        assert fast.assignment.model_dump() == lin.assignment.model_dump(), inc.id
        assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links], inc.id
        decisions.add(lin.assignment.decision)
    if relation is Relation.campaign:
        assert Decision.assign in decisions and Decision.new in decisions


def test_index_equals_linear_scan_with_a_stub_scorer():
    a = [simple(f"a{i}", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)) for i in range(3)]
    b = [simple(f"b{i}", (L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")) for i in range(2)]
    c = [simple(f"c{i}", (L.platform, "P2"), (L.offer, "OC"), (L.creative, "CRC")) for i in range(9)]
    ds = Dataset(name="s", incidents=a + b + c)
    col = link_dataset(ds, backend=stub_backend(ds))
    idx = CandidateIndex.build(col, cache=False)
    for new in [
        simple("n1", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K-new")),
        simple("n2", (L.kit, "K-other"), (L.tenant, "T8")),
        simple("n3", (L.tenant, "T2"), (L.landing, "LA")),
        simple("n4", (L.creative, "CRC")),
    ]:
        lin = link_incident(col, new, backend=stub_backend)
        fast = link_incident(col, new, backend=stub_backend, index=idx)
        assert fast.assignment.model_dump() == lin.assignment.model_dump(), new.id
        assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links]


def test_single_capture_is_assigned_without_reclustering_or_rescanning(monkeypatch):
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    expected = {i.id: link_incident(base, i).assignment.model_dump() for i in new}

    def boom(*a, **k):
        raise AssertionError("the collection was re-clustered or rescanned")

    monkeypatch.setattr(grouping, "group", boom)
    monkeypatch.setattr(grouping, "cluster", boom)
    monkeypatch.setattr(grouping, "link_dataset", boom)
    monkeypatch.setattr(ledger.ScoringContext, "from_dataset", classmethod(boom))
    assign_mod = sys.modules["ftf.linking.assign"]

    monkeypatch.setattr(assign_mod, "combine", boom)
    monkeypatch.setattr(assign_mod, "layer_modalities", boom)
    before = [c.model_dump() for c in base.campaigns]
    for inc in new:
        assert link_incident(base, inc, index=idx).assignment.model_dump() == expected[inc.id]
    assert [c.model_dump() for c in base.campaigns] == before


def test_cached_index_is_used_for_that_base_only():
    clear_cache()
    base, new = _split()
    assert cached_index(base) is None
    idx = CandidateIndex.build(base)
    assert cached_index(base) is idx
    other = Dataset.model_validate(base.model_dump(mode="json"))
    assert cached_index(other) is None
    assert assign(base, new[0]).model_dump() == link_incident(other, new[0]).assignment.model_dump()
    clear_cache()


def test_json_round_trip(tmp_path):
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    rng = np.random.default_rng(0)
    caps = sorted(idx.stage)[:12]
    idx.add_vector_view("ad_image", caps, rng.normal(size=(12, 8)).astype(np.float32))
    path = tmp_path / "index.json"
    idx.save(path)
    assert len(list(tmp_path.glob("index.ad_image-*.npy"))) == 1
    back = CandidateIndex.load(path)
    assert back.postings == idx.postings
    assert {k: back._count(k) for k in back.postings} == {k: idx._count(k) for k in idx.postings}
    assert back.campaigns == idx.campaigns and back.campaign_of == idx.campaign_of
    assert back.stage == idx.stage and back.canonical == idx.canonical
    assert back.modalities == idx.modalities and back.incidents == idx.incidents
    assert back.vectors["ad_image"].capture_ids == caps
    assert np.array_equal(back.vectors["ad_image"].matrix, idx.vectors["ad_image"].matrix)
    assert not back.ready
    for inc in new[:3]:
        assert back.candidates(inc) == idx.candidates(inc)
    with pytest.raises(ValueError):
        back.extend(new[0])
    loaded = CandidateIndex.load(path, base)
    for inc in new[:5]:
        assert link_incident(base, inc, index=loaded).assignment == link_incident(base, inc).assignment
    altered = link_dataset(sc.dataset(sc.corpus(30, seed=99)))
    with pytest.raises(ValueError, match="digest"):
        CandidateIndex.load(path, altered)


def test_candidates_come_from_shared_values_and_vector_neighbours():
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    inc = next(i for i in new if link_incident(base, i).assignment.decision is Decision.assign)
    target = link_incident(base, inc).assignment.campaign_id
    assert target in idx.candidates(inc, k=None)
    # A capture that shares nothing reaches no campaign through values...
    lonely = simple("lonely", (L.landing, "nothing-shared.example"))
    assert idx.candidates(lonely) == []
    # ...but the vector hook adds the campaign of its nearest neighbour (exact top-k by dot product).
    caps = sorted(c for c in idx.campaign_of[Relation.campaign])[:5]
    m = np.eye(5, dtype=np.float32)
    idx.add_vector_view("ad_image", caps, m)
    got = idx.candidates(lonely, vectors={"ad_image": m[3]}, neighbours=1)
    assert got == list(idx.campaign_of[Relation.campaign][caps[3]])
    assert idx.nearest("ad_image", m[3], k=2)[0] == (caps[3], 1.0)
    assert idx.nearest("missing_view", m[0]) == []


def test_cli_assign_with_index_equals_linear(tmp_path):
    incs = sc.corpus(60, seed=8)
    base = link_dataset(sc.dataset(incs[:40]))
    (tmp_path / "linked.json").write_text(base.to_json())
    # NEW.json must validate on its own: keep incidents whose duplicates point inside it
    inside = {c.id for i in incs[40:] for c in i.captures}
    new = [i for i in incs[40:] if all(c.duplicate_of is None or c.duplicate_of in inside for c in i.captures)]
    (tmp_path / "new.json").write_text(sc.dataset(new).to_json())
    assert cli.main(["index", str(tmp_path / "linked.json"), "-o", str(tmp_path / "index.json")]) == 0
    common = ["assign", str(tmp_path / "linked.json"), str(tmp_path / "new.json")]
    assert cli.main(common + ["-o", str(tmp_path / "lin.json"), "--linear"]) == 0
    assert cli.main(common + ["-o", str(tmp_path / "idx.json"), "--index", str(tmp_path / "index.json")]) == 0
    assert cli.main(common + ["-o", str(tmp_path / "mem.json")]) == 0
    lin = Dataset.from_json((tmp_path / "lin.json").read_bytes())
    for name in ("idx.json", "mem.json"):
        got = Dataset.from_json((tmp_path / name).read_bytes())
        assert got.model_dump() == lin.model_dump(), name
    assert len(lin.assignments) == len(new) >= 15


def test_index_refuses_an_incident_already_in_the_base():
    base, _ = _split()
    idx = CandidateIndex.build(base, cache=False)
    with pytest.raises(ValueError, match="already"):
        link_incident(base, base.incidents[0], index=idx)


def test_extend_context_leaves_the_base_context_unchanged():
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    before = _ctx_view(idx._ctx)
    for inc in new:
        extend_context(idx._ctx, idx._extras, inc)
    assert _ctx_view(idx._ctx) == before


# --- review fixes -------------------------------------------------------------------------


def _small_base() -> Dataset:
    b = sc.Builder()

    def inc(iid, vals, stage="lander"):
        cap = b.capture(f"{iid}-l", stage)
        return sc.Incident(id=iid, source="synthetic", captures=[cap], indicators=[b.indicator(cap, k, v, live) for k, v, live in vals])

    camp = [("aio_cdn_tenant", "tq.cdn.example", True), ("aio_landing", "lq.example", True)]
    incs = [inc(f"a{i}", camp) for i in range(3)] + [inc(f"z{i}", [("aio_landing", f"z{i}.example", True)]) for i in range(3)]
    return link_dataset(sc.dataset(incs), [Relation.campaign])


def _dup_incident(iid: str, of: str, stage: str = "lander", extra: bool = False):
    # The caption is campaign-level evidence: the gate needs one under campaign, and the base lacks it,
    # so a0 stays reachable only through values a duplicate merges in.
    b = sc.Builder()
    b.n = 1000
    caps, inds = [], []
    if extra:
        p = b.capture(f"{iid}-p", "lander")
        caps.append(p)
        inds += [b.indicator(p, "meta_pixel", "pxq", True), b.indicator(p, "kit_asset_set", "kit-q", False), b.indicator(p, "ad_caption", "caption-q", False)]
    d = b.capture(f"{iid}-d", stage, duplicate_of=of)
    caps.append(d)
    inds += [b.indicator(d, "meta_pixel", "pxq", True), b.indicator(d, "kit_asset_set", "kit-q", False), b.indicator(d, "ad_caption", "caption-q", False)]
    return sc.Incident(id=iid, source="synthetic", captures=caps, indicators=inds)


def _carrier(iid: str):
    b = sc.Builder()
    b.n = 2000
    p = b.capture(f"{iid}-l", "lander")
    return sc.Incident(id=iid, source="synthetic", captures=[p], indicators=[b.indicator(p, "meta_pixel", "pxq", True), b.indicator(p, "kit_asset_set", "kit-q", False), b.indicator(p, "ad_caption", "caption-q", False)])


def test_values_merged_into_a_member_by_an_added_duplicate_are_reachable():
    base = _small_base()
    i1, i2 = _dup_incident("i1", "a0-l"), _carrier("i2")
    lin1 = link_incident(base, i1)
    lin2 = link_incident(combine(base, [i1]), i2)
    idx = CandidateIndex.build(base, cache=False)
    assert link_incident(base, i1, index=idx).assignment == lin1.assignment
    idx.add(i1)
    fast2 = link_incident(base, i2, index=idx)
    assert lin2.assignment.decision is Decision.assign
    assert fast2.assignment == lin2.assignment and fast2.links == lin2.links


def test_values_merged_by_the_incidents_own_decoy_duplicate_are_reachable():
    base = _small_base()
    inc = _dup_incident("i3", "a0-l", stage="decoy", extra=True)
    lin = link_incident(base, inc)
    assert lin.assignment.decision is Decision.assign
    fast = link_incident(base, inc, index=CandidateIndex.build(base, cache=False))
    assert fast.assignment == lin.assignment and fast.links == lin.links


def test_many_adds_keep_lookups_one_level_deep():
    from collections import ChainMap

    incs = sc.corpus(80, seed=5)
    base = link_dataset(sc.dataset(incs[:20]), [Relation.campaign])
    idx = CandidateIndex.build(base, cache=False)
    known = {c.id for i in incs[:20] for c in i.captures}
    for inc in incs[20:]:
        if all(c.duplicate_of is None or c.duplicate_of in known for c in inc.captures):
            idx.add(inc)
            known |= {c.id for c in inc.captures}
    for name in ("canonical", "incident_of", "indicator_incident", "values", "layers", "index", "group_of"):
        assert not isinstance(getattr(idx._ctx, name), ChainMap), name
    assert not isinstance(idx._extras.live_private, ChainMap)
    full = ScoringContext.from_dataset(combine(base, idx._added))
    assert _ctx_view(idx._ctx) == _ctx_view(full)


def test_index_rejects_reused_capture_or_indicator_ids():
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    taken = base.incidents[0].captures[0]
    clash = sc.Incident(id="clash", source="synthetic", captures=[taken.model_copy()], indicators=[])
    with pytest.raises(ValueError, match="reuses ids"):
        link_incident(base, clash, index=idx)


def test_digest_follows_indicator_values_and_stages():
    base, _ = _split()
    data = base.model_dump(mode="json")
    ind = next(i for i in data["incidents"][0]["indicators"] if i["value"])
    ind["value"] = ind["value"] + "-changed"
    with pytest.raises(ValueError, match="digest"):
        CandidateIndex.build(base, cache=False).attach(Dataset.model_validate(data))


def test_cli_assign_refuses_index_with_linear(tmp_path):
    base, new = _split()
    (tmp_path / "l.json").write_text(base.to_json())
    (tmp_path / "n.json").write_text(sc.dataset(new[:1]).to_json())
    assert cli.main(["assign", str(tmp_path / "l.json"), str(tmp_path / "n.json"), "-o", str(tmp_path / "o.json"), "--linear", "--index", str(tmp_path / "x.json")]) == 2


def test_index_rejects_bad_duplicate_targets_and_reused_artefacts():
    base, new = _split()
    idx = CandidateIndex.build(base, cache=False)
    b = sc.Builder()
    b.n = 5000
    ghost = sc.Incident(id="ghost", source="synthetic", captures=[b.capture("ghost-l", "lander", duplicate_of="no-such-capture")], indicators=[])
    with pytest.raises(ValueError, match="duplicate_of"):
        link_incident(base, ghost, index=idx)
    with pytest.raises(Exception):
        link_incident(base, ghost)  # the linear path rejects it too (validation)
    taken = base.incidents[0].captures[0].artefacts[0]
    cap = b.capture("art-l", "lander").model_copy(update={"artefacts": [taken]})
    reuse = sc.Incident(id="art", source="synthetic", captures=[cap], indicators=[])
    with pytest.raises(ValueError, match="reuses ids"):
        link_incident(base, reuse, index=idx)


def test_cache_is_skipped_for_a_custom_backend_and_an_edited_base():
    clear_cache()
    base, new = _split()
    idx = CandidateIndex.build(base)
    assert cached_index(base) is idx
    base.campaigns.append(base.campaigns[0].model_copy(update={"id": "campaign:campaign:extra"}))
    assert cached_index(base) is None
    clear_cache()


def test_loading_with_another_weight_table_is_refused(tmp_path):
    from dataclasses import replace as dc_replace

    from ftf.linking.weights import HAND_TABLE

    base, _ = _split()
    CandidateIndex.build(base, cache=False).save(tmp_path / "i.json")
    CandidateIndex.load(tmp_path / "i.json")  # same table: fine
    with pytest.raises(ValueError, match="weight table"):
        CandidateIndex.load(tmp_path / "i.json", weights=dc_replace(HAND_TABLE, hub_bits=0.7))


def test_an_index_for_another_base_is_refused():
    base, new = _split()
    other, _ = _split(seed=99)
    idx = CandidateIndex.build(other, cache=False)
    with pytest.raises(ValueError, match="another base"):
        link_incident(base, new[0], index=idx)


# --- options: learned weights, background rarity, chain rule ---------------------------------


def _options_case(seed: int = 7):
    """A chain-rule corpus (tenant ⊃ owner ⊃ aio campaign ⊃ landing), learned weights, background counts
    from another synthetic corpus that shares some values, and the base linked with those options."""
    from ftf.linking.background import BackgroundCounts
    from ftf.linking.backend import Backend
    from ftf.linking.learned import learn_weights

    incs = sc.corpus(70, seed=seed, chain=True)
    k = 48
    known = {c.id for i in incs[:k] for c in i.captures}
    new = [i for i in incs[k:] if all(c.duplicate_of is None or c.duplicate_of in known for c in i.captures)]
    bg = BackgroundCounts.from_dataset(sc.dataset(sc.corpus(40, seed=seed + 100, chain=True)))
    table, _ = learn_weights(sc.dataset(incs[:k]), background=bg)

    def factory(ds):
        return Backend.from_context(ScoringContext.from_dataset(ds, table, background=bg))

    raw = sc.dataset(incs[:k])
    base = link_dataset(raw, backend=factory(raw))
    return base, new, table, bg, factory


@pytest.mark.parametrize("seed", [7, 8])
def test_extended_context_equals_a_rebuild_with_options(seed):
    base, new, table, bg, _ = _options_case(seed)
    idx = CandidateIndex.build(base, weights=table, background=bg, cache=False)
    assert idx._ctx.hierarchy is not None and idx._ctx.hierarchy.level_of
    for inc in new:
        full = ScoringContext.from_dataset(combine(base, [inc]), table, background=bg)
        assert _ctx_view(idx.extend(inc)) == _ctx_view(full), inc.id


@pytest.mark.parametrize("seed", [7, 8])
def test_index_equals_linear_with_learned_weights_background_and_chain_rule(seed):
    """Every relation, single incidents and the `ftf assign` sequence (index.add between incidents)."""
    base, new, table, bg, factory = _options_case(seed)
    assert table is not ledger.HAND_TABLE
    decisions = set()
    for relation in Relation:
        idx = CandidateIndex.build(base, weights=table, background=bg, cache=False)  # never grown
        seq = CandidateIndex.build(base, weights=table, background=bg, cache=False)  # grown with add()
        current = base
        for inc in new:
            lin = link_incident(base, inc, relation, backend=factory)
            fast = link_incident(base, inc, relation, index=idx)
            assert fast.assignment.model_dump() == lin.assignment.model_dump(), (relation, inc.id)
            assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links], (relation, inc.id)
            decisions.add(lin.assignment.decision)
            # sequence: the index after add() == the linear scan over the grown collection
            seq_lin = link_incident(current, inc, relation, backend=factory)
            seq_fast = link_incident(base, inc, relation, index=seq)
            assert seq_fast.assignment.model_dump() == seq_lin.assignment.model_dump(), (relation, inc.id, "seq")
            seq.add(inc)
            current = combine(current, [inc])
    assert Decision.assign in decisions


def test_the_owner_rule_is_shared_with_the_ledger():
    """An aio campaign id in the owner layer feeds no pre-group, in the ledger and in the extension alike."""
    base, new, table, bg, _ = _options_case()
    idx = CandidateIndex.build(base, weights=table, background=bg, cache=False)
    assert all(not v.startswith("aio_campaign=") for vs in idx._extras.owners.values() for v in vs)
    inc = next(i for i in new if any(x.kind == "aio_campaign" for x in i.indicators))
    _, ex = extend_context(idx._ctx, idx._extras, inc)
    assert all(not v.startswith("aio_campaign=") for vs in ex.owners.values() for v in vs)


def test_upper_bound_uses_the_background_rarity():
    """With background counts the rarity term is below log2(groups): a value the hand bound would drop as a
    hub must still propose its campaigns."""
    import math

    base, new, table, bg, _ = _options_case()
    idx = CandidateIndex.build(base, weights=table, background=bg, cache=False)
    ctx = idx._ctx
    lower = [key for key in idx.postings if ctx.groups(key) > 1 and ctx.rarity(key, ctx.groups(key)) < math.log2(ctx.groups(key))]
    assert lower, "the case should have values whose background rarity is below log2(groups)"
    for key in lower:
        from ftf.linking.index import _prior_cap

        assert idx._upper_bits(key, Relation.campaign) == pytest.approx(
            _prior_cap(table, key[1], key[0], Relation.campaign) - ctx.rarity(key, ctx.groups(key))
        )


def test_loading_with_other_options_is_refused(tmp_path):
    base, new, table, bg, factory = _options_case()
    idx = CandidateIndex.build(base, weights=table, background=bg, cache=False)
    idx.save(tmp_path / "i.json")
    loaded = CandidateIndex.load(tmp_path / "i.json", base, weights=table, background=bg)
    assert link_incident(base, new[0], index=loaded).assignment == link_incident(base, new[0], backend=factory).assignment
    for kwargs in ({"weights": table}, {"weights": table, "background": bg, "chain_rule": False}, {"background": bg}):
        with pytest.raises(ValueError, match="another weight table, background or chain rule"):
            CandidateIndex.load(tmp_path / "i.json", **kwargs)


def test_the_digest_covers_the_indicator_level():
    from ftf.contract.models import Level
    from ftf.linking.index import dataset_digest

    base, _ = _split()
    data = base.model_dump(mode="json")
    data["incidents"][0]["indicators"][0]["level"] = Level.operator.value if base.incidents[0].indicators[0].level is not Level.operator else Level.tenant.value
    assert dataset_digest(Dataset.model_validate(data)) != dataset_digest(base)


def test_only_a_default_options_index_is_cached():
    clear_cache()
    base, new, table, bg, _ = _options_case()
    CandidateIndex.build(base, weights=table, background=bg)
    assert cached_index(base) is None  # link_incident(base, inc) must not pick up the learned table
    clear_cache()


def test_weights_giving_bits_to_a_dropped_kind_fall_back_to_the_linear_scan(monkeypatch):
    """The index keeps no postings for kinds without bits; a table that gives them bits scans linearly."""
    from dataclasses import replace as dc_replace

    from ftf.linking.weights import HAND_TABLE

    base, new = _split()
    # an index whose table gives the server IP no bits under any relation
    no_ip = dc_replace(HAND_TABLE, kinds={**HAND_TABLE.kinds, "server_ip": dc_replace(HAND_TABLE.kinds["server_ip"], bits=0.0)})
    idx = CandidateIndex.build(base, weights=no_ip, cache=False)
    assert (L.infrastructure, "server_ip") in idx.dropped
    assert idx.drops_bits_of(HAND_TABLE) and not idx.drops_bits_of(no_ip)
    for inc in new[:6]:
        for relation in (Relation.campaign, Relation.operator):
            lin = link_incident(base, inc, relation, weights=HAND_TABLE)
            fast = link_incident(base, inc, relation, index=idx, weights=HAND_TABLE)
            assert fast.assignment.model_dump() == lin.assignment.model_dump(), inc.id


def test_candidate_note_names_the_conflicting_kind():
    from ftf.contract.models import EvidenceRow, EvidenceStatus
    from ftf.linking.assign import _candidate_note
    from ftf.linking.types import PairScore

    row = EvidenceRow(layer=L.owner, kind="aio_campaign", status=EvidenceStatus.conflict, value="a vs b", bits=-4.0)
    ps = PairScore(a="x", b="y", relation=Relation.campaign, bits=-4.0, rows=[row])
    assert "conflict: owner aio_campaign" in _candidate_note(ps)


def _multi_lander_cases():
    from linking_support import cap, incident

    x = [(L.platform, "P1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)]
    cases = {
        "disagree": [(L.platform, "P1"), (L.landing, "LB"), (L.kit, "K2")],
        "agree": [(L.platform, "P1"), (L.landing, "LA"), (L.kit, "K1")],
        "nowhere": [(L.offer, "O-unrelated")],
    }
    out = [
        incident(f"n-{k}", [cap(f"n-{k}-ad", "ad"), cap(f"n-{k}-x"), cap(f"n-{k}-y")], {f"n-{k}-ad": [(L.creative, "CR-n")], f"n-{k}-x": x, f"n-{k}-y": y})
        for k, y in cases.items()
    ]
    # an ad matching another campaign than the landers point to
    out.append(
        incident(
            "n-off",
            [cap("n-off-ad", "ad"), cap("n-off-x"), cap("n-off-y")],
            {"n-off-ad": [(L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")], "n-off-x": x, "n-off-y": [(L.offer, "O-unrelated")]},
        )
    )
    # one lander duplicates a member of campaign B, the other matches campaign A
    out.append(incident("n-dup", [cap("n-dup-x"), cap("n-dup-y", duplicate_of="b0-c1")], {"n-dup-x": x, "n-dup-y": [(L.landing, "LB")]}))
    return out


@pytest.mark.parametrize("stub", [True, False])
def test_index_equals_linear_scan_for_incidents_with_several_landers(stub):
    a = [simple(f"a{i}", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)) for i in range(3)]
    b = [simple(f"b{i}", (L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")) for i in range(2)]
    c = [simple(f"c{i}", (L.platform, "P2"), (L.offer, "OC"), (L.creative, "CRC")) for i in range(9)]
    ds = Dataset(name="s", incidents=a + b + c)
    col = link_dataset(ds, backend=stub_backend(ds)) if stub else link_dataset(ds)
    idx = CandidateIndex.build(col, cache=False)
    backend = stub_backend if stub else None
    notes = {}
    for new in _multi_lander_cases():
        lin = link_incident(col, new, backend=backend)  # no cached index for `col`: the linear scan
        fast = link_incident(col, new, backend=backend, index=idx)
        assert fast.assignment.model_dump() == lin.assignment.model_dump(), new.id
        assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links], new.id
        notes[new.id] = (lin.assignment.decision, lin.assignment.note)
    if stub:
        assert notes["n-disagree"][0] is Decision.review and "landers disagree" in notes["n-disagree"][1]
        assert notes["n-agree"][0] is Decision.assign
        assert notes["n-off"][0] is Decision.review
        assert notes["n-dup"][0] is Decision.review and "landers disagree" in notes["n-dup"][1]
