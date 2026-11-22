from __future__ import annotations

import json
import math

from ftf.contract.models import Dataset, Decision, Layer, Relation
from ftf.linking import assign, link_dataset, link_incident
from ftf.linking.assign import combine

from linking_support import TAU, WEIGHTS, cap, dataset, incident, simple, stub_backend

L = Layer


def _collection() -> Dataset:
    a = [simple(f"a{i}", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)) for i in range(3)]
    b = [simple(f"b{i}", (L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")) for i in range(2)]
    c = [simple(f"c{i}", (L.platform, "P2"), (L.offer, "OC"), (L.creative, "CRC")) for i in range(2)]
    ds = dataset(*a, *b, *c)
    return link_dataset(ds, backend=stub_backend(ds))


def _k(collection: Dataset, relation=Relation.campaign) -> int:
    return sum(1 for c in collection.campaigns if c.relation is relation)


def test_assign_known_variant():
    col = _collection()
    new = simple("n", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K-new"), (L.tracking_account, "PXA", True))
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.assign
    camp = next(c for c in col.campaigns if c.id == a.campaign_id)
    assert set(camp.capture_ids) == {f"a{i}-c1" for i in range(3)}
    assert a.candidates[0].campaign_id == a.campaign_id
    assert a.candidates[0].score_bits >= TAU + math.log2(_k(col))
    assert "tenant" in a.candidates[0].note


def test_new_when_nothing_matches():
    col = _collection()
    new = simple("n", (L.platform, "P9"), (L.tenant, "T9"), (L.landing, "L9"), (L.kit, "K9"))
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.new
    assert a.campaign_id is None


def test_review_between_the_bars():
    col = _collection()
    # Shares the landing and kit with A but has another tenant: 4 + 3 + 1.5 - 4 = 4.5 bits, between tau-3 and the bar.
    new = simple("n", (L.platform, "P1"), (L.tenant, "T7"), (L.landing, "LA"), (L.kit, "K1"))
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.review
    assert TAU - 3 < a.candidates[0].score_bits < TAU + math.log2(_k(col))


def test_bar_grows_with_number_of_campaigns():
    col = _collection()
    # 6.5 bits: above tau, but below tau + log2 K for K = 3 single-relation campaigns -> review.
    new = simple("n", (L.offer, "OC"), (L.creative, "CRC"), (L.platform, "P2"), (L.kit, "Kx"))
    a = assign(col, new, backend=stub_backend)
    assert _k(col) >= 3
    assert TAU <= a.candidates[0].score_bits < TAU + math.log2(_k(col))
    assert a.decision is Decision.review


def test_missing_modalities_never_lower_the_score_and_are_reported():
    col = _collection()
    full = simple("n", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"))
    no_har = simple("n", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), arts=("html", "ad_screenshot"), missing=[("har", "not captured")])
    a_full = assign(col, full, backend=stub_backend)
    a_nohar = assign(col, no_har, backend=stub_backend)
    assert a_nohar.candidates[0].score_bits >= a_full.candidates[0].score_bits
    assert a_nohar.decision is a_full.decision
    assert "har missing (not captured): up to +" in a_nohar.note
    assert a_nohar.evidence_capacity_bits < a_full.evidence_capacity_bits


def test_capacity_comes_from_the_weight_table():
    col = _collection()
    shot_only = simple("n", (L.creative, "CR-unknown"), arts=("ad_screenshot",), missing=[("html", "page dead"), ("har", "page dead")])
    a = assign(col, shot_only, backend=stub_backend)
    assert a.evidence_capacity_bits == WEIGHTS[L.creative]
    # Capacity is below tau, so the item cannot prove a new campaign: review, not new.
    assert a.decision is Decision.review
    assert "html missing (page dead): up to +" in a.note


def test_link_incident_links_are_contract_valid():
    col = _collection()
    new = simple("n", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"))
    res = link_incident(col, new, backend=stub_backend)
    assert res.links and all("n-c1" in (l.a, l.b) for l in res.links)
    out = combine(col, [new])
    data = out.model_dump(mode="json")
    data["links"] += [l.model_dump(mode="json") for l in res.links]
    data["assignments"] = [res.assignment.model_dump(mode="json")]
    Dataset.model_validate(json.loads(json.dumps(data)))


def test_assign_does_not_recluster():
    col = _collection()
    before = [c.model_dump() for c in col.campaigns]
    assign(col, simple("n", (L.tenant, "T1"), (L.landing, "LA")), backend=stub_backend)
    assert [c.model_dump() for c in col.campaigns] == before


def test_cli_link_and_assign(tmp_path, monkeypatch):
    from ftf import cli
    from ftf.linking import backend as backend_mod

    # The stub replaces Backend.from_dataset, which only the linear scan (--linear) calls; the
    # indexed path extends the real ledger context (covered in test_index.py).
    monkeypatch.setattr(backend_mod.Backend, "from_dataset", classmethod(lambda cls, ds: stub_backend(ds)))
    a = [simple(f"a{i}", (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1")) for i in range(2)]
    (tmp_path / "in.json").write_text(dataset(*a).to_json())
    (tmp_path / "new.json").write_text(dataset(simple("n", (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1")), simple("m", (L.kit, "K-other"), (L.tenant, "T8"), (L.landing, "L8"))).to_json())
    assert cli.main(["link", str(tmp_path / "in.json"), "-o", str(tmp_path / "linked.json")]) == 0
    assert cli.main(["assign", str(tmp_path / "linked.json"), str(tmp_path / "new.json"), "-o", str(tmp_path / "out.json"), "--linear"]) == 0
    out = Dataset.from_json((tmp_path / "out.json").read_bytes())
    decisions = {a.incident_id: a.decision for a in out.assignments}
    assert decisions == {"n": Decision.assign, "m": Decision.new}


def test_conflict_on_a_lander_is_not_hidden_by_an_empty_ad_pair():
    from linking_support import cap, incident

    col = _collection()
    caps = [cap("n-ad", "ad", arts=("ad_screenshot",)), cap("n-l", "lander")]
    new = incident("n", caps, {"n-ad": [(L.creative, "CR-other")], "n-l": [(L.platform, "P1"), (L.tenant, "T9")]})
    a = assign(col, new, backend=stub_backend)
    by = {c.campaign_id: c.score_bits for c in a.candidates}
    a_id = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "a0-c1" in c.capture_ids)
    assert by[a_id] < 0  # platform +1.5, tenant conflict -4
    assert "conflict: tenant" in next(c.note for c in a.candidates if c.campaign_id == a_id)


def test_exact_duplicate_of_a_collection_capture_is_assigned_to_its_campaign():
    from linking_support import cap, incident

    x = simple("x", (L.tenant, "T1"), (L.landing, "LA"))
    ds = dataset(x)
    col = link_dataset(ds, backend=stub_backend(ds))
    new = incident("n", [cap("n-c1", duplicate_of="x-c1")], {"n-c1": [(L.tenant, "T1"), (L.landing, "LA")]})
    res = link_incident(col, new, backend=stub_backend)
    camp = next(c for c in col.campaigns if c.relation is Relation.campaign and "x-c1" in c.capture_ids)
    assert res.assignment.decision is Decision.assign
    assert res.assignment.campaign_id == camp.id
    assert "exact duplicate" in res.assignment.note
    assert all("n-c1" in (l.a, l.b) for l in res.links)  # never a link between two collection captures


def test_assign_target_is_best_qualifying_campaign_not_top_score():
    g1 = [simple(f"g{i}", (L.tenant, "T1"), (L.landing, "L1")) for i in range(2)]
    g2 = [simple(f"h{i}", (L.kit, "K2"), (L.offer, "O2"), (L.infrastructure, "I2")) for i in range(2)]
    ds = dataset(*g1, *g2)
    col = link_dataset(ds, backend=stub_backend(ds))

    def low_tau(d):
        be = stub_backend(d)
        be.threshold = lambda relation: 2.0
        return be

    # vs g: tenant only, 5 bits on one layer; vs h: offer + infrastructure, 3 bits on two layers.
    new = simple("n", (L.tenant, "T1"), (L.offer, "O2"), (L.infrastructure, "I2"))
    a = assign(col, new, backend=low_tau)
    h = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "h0-c1" in c.capture_ids)
    k = sum(1 for c in col.campaigns if c.relation is Relation.campaign)
    assert 2.0 + math.log2(k) <= 3.0
    assert a.candidates[0].campaign_id != h
    assert a.decision is Decision.assign and a.campaign_id == h


def test_cli_assign_refuses_relation_without_campaigns(tmp_path, monkeypatch, capsys):
    from ftf import cli
    from ftf.linking import backend as backend_mod

    monkeypatch.setattr(backend_mod.Backend, "from_dataset", classmethod(lambda cls, ds: stub_backend(ds)))
    (tmp_path / "in.json").write_text(dataset(simple("a", (L.kit, "K1"))).to_json())
    (tmp_path / "new.json").write_text(dataset(simple("n", (L.kit, "K1"))).to_json())
    assert cli.main(["link", str(tmp_path / "in.json"), "-o", str(tmp_path / "l.json"), "--relation", "template", "--relation", "template"]) == 0
    assert capsys.readouterr().out.count("  template:") == 1
    assert cli.main(["assign", str(tmp_path / "l.json"), str(tmp_path / "new.json"), "-o", str(tmp_path / "o.json")]) == 2
    assert not (tmp_path / "o.json").exists()


def test_tenant_conflict_on_another_capture_blocks_assignment():
    from linking_support import cap, incident

    col = _collection()
    caps = [cap("n-l1"), cap("n-l2")]
    vals = {
        "n-l1": [(L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)],
        "n-l2": [(L.tenant, "T5")],
    }
    a = assign(col, incident("n", caps, vals), backend=stub_backend)
    assert a.decision is Decision.review
    assert "tenant/owner conflict" in a.note
    # Template has no cannot-link: the same incident is not blocked there.
    t = assign(col, incident("n", caps, vals), relation=Relation.template, backend=stub_backend)
    assert "tenant/owner conflict" not in t.note


def test_duplicate_shortcut_does_not_override_a_conflict():
    from linking_support import cap, incident

    x = simple("x", (L.tenant, "T1"), (L.landing, "LA"))
    ds = dataset(x)
    col = link_dataset(ds, backend=stub_backend(ds))
    new = incident("n", [cap("n-c1", duplicate_of="x-c1"), cap("n-c2")], {"n-c1": [(L.tenant, "T1"), (L.landing, "LA")], "n-c2": [(L.tenant, "T2")]})
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.review


def test_cli_assign_handles_new_incidents_duplicating_each_other(tmp_path, monkeypatch):
    from ftf import cli
    from ftf.linking import backend as backend_mod
    from linking_support import cap, incident

    monkeypatch.setattr(backend_mod.Backend, "from_dataset", classmethod(lambda cls, ds: stub_backend(ds)))
    a = [simple(f"a{i}", (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1")) for i in range(2)]
    (tmp_path / "in.json").write_text(dataset(*a).to_json())
    first = incident("n2", [cap("n2-c1", duplicate_of="n1-c1")], {"n2-c1": [(L.tenant, "T8"), (L.landing, "L8")]})
    second = simple("n1", (L.tenant, "T8"), (L.landing, "L8"), (L.kit, "K8"))
    (tmp_path / "new.json").write_text(Dataset.model_construct(contract_version="0.1.0", name="n", description=None, incidents=[first, second], links=[], campaigns=[], assignments=[]).model_dump_json())
    assert cli.main(["link", str(tmp_path / "in.json"), "-o", str(tmp_path / "linked.json")]) == 0
    assert cli.main(["assign", str(tmp_path / "linked.json"), str(tmp_path / "new.json"), "-o", str(tmp_path / "out.json"), "--linear"]) == 0
    out = Dataset.from_json((tmp_path / "out.json").read_bytes())
    decisions = {a.incident_id: a.decision for a in out.assignments}
    assert decisions == {"n1": Decision.new, "n2": Decision.review}  # a duplicate of a capture in no campaign


# --- incidents with several landers (HACK-57) -------------------------------------------------


def _two_landers(y_vals):
    vals = {
        "n-x": [(L.platform, "P1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)],
        "n-y": y_vals,
    }
    return incident("n", [cap("n-ad", "ad"), cap("n-x"), cap("n-y")], {"n-ad": [(L.creative, "CR-n")], **vals})


def test_landers_pointing_to_different_campaigns_go_to_review():
    col = _collection()
    new = _two_landers([(L.platform, "P1"), (L.landing, "LB"), (L.kit, "K2")])
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.review
    assert a.campaign_id is None
    camp_a = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "a0-c1" in c.capture_ids)
    camp_b = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "b0-c1" in c.capture_ids)
    assert "landers disagree" in a.note
    assert f"n-x -> {camp_a}" in a.note and f"n-y -> {camp_b}" in a.note


def test_landers_agreeing_or_one_pointing_nowhere_still_assign():
    col = _collection()
    camp_a = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "a0-c1" in c.capture_ids)
    for y_vals in ([(L.platform, "P1"), (L.landing, "LA"), (L.kit, "K1")], [(L.offer, "O-unrelated")]):
        a = assign(col, _two_landers(y_vals), backend=stub_backend)
        assert a.decision is Decision.assign, y_vals
        assert a.campaign_id == camp_a
        assert "landers disagree" not in a.note


def test_ad_match_elsewhere_than_the_landers_point_goes_to_review():
    col = _collection()
    camp_a = next(c.id for c in col.campaigns if c.relation is Relation.campaign and "a0-c1" in c.capture_ids)
    vals = {
        "n-ad": [(L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")],  # 13.5 bits against B
        "n-x": [(L.platform, "P1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True)],  # 12.5 against A
        "n-y": [(L.offer, "O-unrelated")],
    }
    new = incident("n", [cap("n-ad", "ad"), cap("n-x"), cap("n-y")], vals)
    a = assign(col, new, backend=stub_backend)
    assert a.decision is Decision.review
    assert f"n-x -> {camp_a}" in a.note


# --- tie at the top (HACK-64) ---------------------------------------------------------


def _two_campaigns() -> Dataset:
    # Two campaigns that share nothing with each other; stub bits: landing 4, kit 3, offer 2,
    # platform 1.5, infrastructure 1. K = 2, so the assign bar is TAU + 1 = 7 bits.
    a = [simple(f"a{i}", (L.platform, "P1"), (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA"), (L.infrastructure, "IA")) for i in range(3)]
    b = [simple(f"b{i}", (L.landing, "LB"), (L.kit, "KB"), (L.offer, "OB"), (L.infrastructure, "IB")) for i in range(2)]
    ds = dataset(*a, *b)
    return link_dataset(ds, backend=stub_backend(ds))


def _camp(col: Dataset, member: str) -> str:
    return next(c.id for c in col.campaigns if c.relation is Relation.campaign and member in c.capture_ids)


TIE_CASES = {
    # equal scores: 9 bits against each campaign
    "tie": simple("n", (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA"), (L.landing, "LB"), (L.kit, "KB"), (L.offer, "OB")),
    # near tie: a 8.5 (landing, kit, platform) vs b 8 (landing, kit, infrastructure), lead 0.5 bit
    "near": simple("n", (L.platform, "P1"), (L.landing, "LA"), (L.kit, "KA"), (L.landing, "LB"), (L.kit, "KB"), (L.infrastructure, "IB")),
    # clear winner: a 9 vs b 7 (both clear the bar), lead 2 bits
    "clear": simple("n", (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA"), (L.landing, "LB"), (L.kit, "KB")),
    # lead exactly the margin: a 10 vs b 9, lead 1 bit, still assign
    "margin": simple("n", (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA"), (L.infrastructure, "IA"), (L.landing, "LB"), (L.kit, "KB"), (L.offer, "OB")),
}


def test_tie_at_the_top_goes_to_review_naming_both():
    col = _two_campaigns()
    ca, cb = _camp(col, "a0-c1"), _camp(col, "b0-c1")
    assert _k(col) == 2
    for case in ("tie", "near"):
        a = assign(col, TIE_CASES[case], backend=stub_backend)
        assert a.decision is Decision.review, case
        assert a.campaign_id is None
        assert "tie at the top" in a.note and ca in a.note and cb in a.note, case
        assert {ca, cb} <= {c.campaign_id for c in a.candidates}
        top = sorted((c.score_bits for c in a.candidates), reverse=True)[:2]
        assert top[0] - top[1] < 1.0 and top[1] >= TAU + 1


def test_clear_winner_is_still_assigned():
    col = _two_campaigns()
    ca = _camp(col, "a0-c1")
    for case in ("clear", "margin"):
        a = assign(col, TIE_CASES[case], backend=stub_backend)
        assert a.decision is Decision.assign, case
        assert a.campaign_id == ca
        assert "tie at the top" not in a.note


def test_tie_rule_is_the_same_on_the_indexed_path():
    from ftf.linking.index import CandidateIndex

    col = _two_campaigns()
    idx = CandidateIndex.build(col, cache=False)
    for case, inc in TIE_CASES.items():
        lin = link_incident(col, inc, backend=stub_backend)
        fast = link_incident(col, inc, backend=stub_backend, index=idx)
        assert fast.assignment.model_dump() == lin.assignment.model_dump(), case
        assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links], case


def test_tie_rule_is_relation_agnostic_template():
    # The template relation (kit, landing, creative, offer) applies the same margin.
    a = [simple(f"a{i}", (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA")) for i in range(2)]
    b = [simple(f"b{i}", (L.landing, "LB"), (L.kit, "KB")) for i in range(2)]
    ds = dataset(*a, *b)
    col = link_dataset(ds, [Relation.template], backend=stub_backend(ds))
    assert _k(col, Relation.template) == 2  # bar = TAU + 1 = 7
    tie = simple("n", (L.landing, "LA"), (L.kit, "KA"), (L.landing, "LB"), (L.kit, "KB"))
    t = assign(col, tie, relation=Relation.template, backend=stub_backend)
    assert t.decision is Decision.review and "tie at the top" in t.note
    # a 9 vs b 7: the rival also clears the bar, but trails by 2 bits
    win = simple("n", (L.landing, "LA"), (L.kit, "KA"), (L.offer, "OA"), (L.landing, "LB"), (L.kit, "KB"))
    w = assign(col, win, relation=Relation.template, backend=stub_backend)
    assert w.decision is Decision.assign


def _har_layers_collection() -> Dataset:
    # Like `_collection`, but campaign A also carries an infrastructure value (a HAR-only layer),
    # so a missing HAR could add tracking account + infrastructure = 5 bits.
    a = [simple(f"a{i}", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, "PXA", True), (L.infrastructure, "IA")) for i in range(3)]
    b = [simple(f"b{i}", (L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")) for i in range(2)]
    c = [simple(f"c{i}", (L.platform, "P2"), (L.offer, "OC"), (L.creative, "CRC")) for i in range(2)]
    ds = dataset(*a, *b, *c)
    return link_dataset(ds, backend=stub_backend(ds))


NO_HAR = dict(arts=("html", "ad_screenshot"), missing=[("har", "not captured")])
# Shares only the kit with A: 3 bits, at the new cut (tau - 3).
WEAK_MATCH = ((L.kit, "K1"), (L.landing, "L9"), (L.offer, "O9"))
NO_MATCH = ((L.kit, "K9"), (L.landing, "L9"), (L.offer, "O9"))


def test_missing_inputs_that_could_reach_the_bar_give_review_not_new():
    col = _har_layers_collection()
    bar = TAU + math.log2(_k(col))
    full = assign(col, simple("n", *WEAK_MATCH), backend=stub_backend)
    assert full.decision is Decision.new
    no_har = assign(col, simple("n", *WEAK_MATCH, **NO_HAR), backend=stub_backend)
    assert no_har.candidates[0].score_bits == full.candidates[0].score_bits == TAU - 3
    assert no_har.candidates[0].score_bits + 5.0 >= bar
    assert no_har.decision is Decision.review
    assert "missing inputs could reach the assign bar" in no_har.note
    assert "har (not captured)" in no_har.note


def test_missing_inputs_that_cannot_reach_the_bar_keep_new():
    col = _har_layers_collection()
    a = assign(col, simple("n", *NO_MATCH, **NO_HAR), backend=stub_backend)
    assert a.decision is Decision.new
    assert "missing inputs could reach the assign bar" not in a.note
    assert "har missing (not captured): up to +5.0" in a.note


def test_missing_input_rule_is_the_same_on_the_indexed_path():
    from ftf.linking.index import CandidateIndex

    col = _har_layers_collection()
    idx = CandidateIndex.build(col, cache=False)
    decisions = set()
    for vals in (WEAK_MATCH, NO_MATCH):
        for extra in ({}, NO_HAR):
            inc = simple("n", *vals, **extra)
            lin = link_incident(col, inc, backend=stub_backend)
            fast = link_incident(col, inc, backend=stub_backend, index=idx)
            assert fast.assignment.model_dump() == lin.assignment.model_dump(), (vals, extra)
            assert [l.model_dump() for l in fast.links] == [l.model_dump() for l in lin.links]
            decisions.add(lin.assignment.decision)
    assert decisions == {Decision.new, Decision.review}


def test_layer_caps_per_kind_follow_the_modalities_present():
    from ftf.contract.models import ArtefactType as A
    from ftf.linking import HAND_TABLE
    from ftf.linking.backend import layer_caps

    full = layer_caps(HAND_TABLE, Relation.campaign)
    page = layer_caps(HAND_TABLE, Relation.campaign, frozenset({A.html, A.asset, A.ad_screenshot}))
    traffic = layer_caps(HAND_TABLE, Relation.campaign, frozenset({A.html, A.har}))
    assert traffic == full
    # Without recorded traffic a private id is only seen in saved files: the ledger's copy discount.
    assert page[L.tenant] == full[L.tenant] - HAND_TABLE.static_discount
    # Postbacks and redirect hops need traffic; the static kinds of the layer still count.
    assert page[L.tracker_instance] < full[L.tracker_instance]
    assert all(page.get(layer, 0.0) <= cap for layer, cap in full.items())
    # Unlisted kinds come from any modality: a screenshot alone keeps its creative cap.
    shot = layer_caps(HAND_TABLE, Relation.campaign, frozenset({A.ad_screenshot}))
    assert shot[L.creative] == full[L.creative]


def test_capacity_per_kind_reports_a_missing_har_inside_reachable_layers():
    from ftf.contract.models import ArtefactType as A
    from ftf.linking.assign import evidence_capacity, layer_modalities

    col = _har_layers_collection()
    inc = simple("n", *NO_MATCH, **NO_HAR)
    lm = layer_modalities(combine(col, [inc]))
    probe = [c.id for c in inc.captures]

    def mod_caps(mods):
        # Every layer reachable from html, but the tenant id is worth 2 bits more with traffic.
        return {**WEIGHTS, L.tenant: WEIGHTS[L.tenant] - (0.0 if A.har in mods else 2.0)}

    lm_all_html = {layer: mods | {A.html} for layer, mods in lm.items()}
    cap_layer, gains_layer, extra_layer = evidence_capacity(None, inc, probe, WEIGHTS, lm=lm_all_html)
    assert gains_layer == [] and extra_layer == 0.0
    cap_kind, gains_kind, extra_kind = evidence_capacity(None, inc, probe, WEIGHTS, lm=lm_all_html, modality_caps=mod_caps)
    assert cap_kind == cap_layer - 2.0
    assert extra_kind == 2.0
    assert [(g[0], g[1], g[2], g[3]) for g in gains_kind] == [(A.har, "not captured", 2.0, [L.tenant])]
