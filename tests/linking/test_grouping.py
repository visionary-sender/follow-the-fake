from __future__ import annotations

import json

from ftf.contract.models import Dataset, EvidenceRow, EvidenceStatus, Layer, Relation
from ftf.linking import cluster, group, link_dataset
from ftf.linking.types import PairScore

from linking_support import TAU, cap, dataset, incident, simple, stub_backend

L = Layer


def ps(a, b, bits, layers=2, live=False, conflict=False):
    rows = [EvidenceRow(layer=L.tenant, kind="tenant_id", status=EvidenceStatus.conflict, bits=-4.0)] if conflict else []
    return PairScore(a=a, b=b, relation=Relation.campaign, bits=bits, rows=rows, layers_positive=layers, live_private=live)


# --- generic core -------------------------------------------------------------------------


def test_cluster_merges_strong_edges_and_keeps_singletons():
    out = cluster(["a", "b", "c", "d"], [ps("a", "b", 12), ps("b", "c", 10), ps("a", "c", 9)], TAU)
    assert out == [{"a", "b", "c"}, {"d"}]


def test_edge_needs_two_layers_or_live_private():
    one_layer = cluster(["a", "b"], [ps("a", "b", 20, layers=1)], TAU)
    assert one_layer == [{"a"}, {"b"}]
    live = cluster(["a", "b"], [ps("a", "b", 7, layers=1, live=True)], TAU)
    assert live == [{"a", "b"}]
    below = cluster(["a", "b"], [ps("a", "b", TAU - 0.1, layers=3)], TAU)
    assert below == [{"a"}, {"b"}]


def test_cannot_link_is_respected_even_through_a_third_node():
    scores = [ps("a", "b", 12), ps("b", "c", 12), ps("a", "c", 12)]
    out = cluster(["a", "b", "c"], scores, TAU, cannot_link={frozenset({"a", "c"})})
    assert {frozenset(c) for c in out} == {frozenset({"a", "b"}), frozenset({"c"})}


def test_single_false_private_match_does_not_chain_two_campaigns():
    # Two tight groups of three; one strong false edge between a1 and b1.
    a, b = ["a1", "a2", "a3"], ["b1", "b2", "b3"]
    scores = [ps(x, y, 14) for g in (a, b) for i, x in enumerate(g) for y in g[i + 1 :]]
    scores.append(ps("a1", "b1", 12, layers=1, live=True))
    out = cluster(a + b, scores, TAU)
    assert {frozenset(c) for c in out} == {frozenset(a), frozenset(b)}


def test_must_link_units_and_split_on_conflict():
    out = cluster(["x1", "x2", "y"], [], TAU, must_link=[["x1", "x2"]])
    assert {frozenset(c) for c in out} == {frozenset({"x1", "x2"}), frozenset({"y"})}
    out = cluster(["x1", "x2"], [], TAU, cannot_link={frozenset({"x1", "x2"})}, must_link=[["x1", "x2"]])
    assert {frozenset(c) for c in out} == {frozenset({"x1"}), frozenset({"x2"})}


def test_cluster_is_deterministic():
    scores = [ps("a", "b", 10), ps("c", "d", 10), ps("b", "c", 10)]
    assert cluster("abcd", scores, TAU) == cluster("dcba", list(reversed(scores)), TAU)


# --- dataset level ------------------------------------------------------------------------


def _two_campaigns_and_a_copy() -> Dataset:
    # Campaign A: tenant T1, landing LA, kit K1 (3 incidents). Campaign B: tenant T2, landing LB, kit K2.
    # Copy: kit K1 and landing LA copied onto tenant T9 (a copied kit).
    a = [simple(f"a{i}", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"), (L.tracking_account, f"PX{i}")) for i in range(3)]
    b = [simple(f"b{i}", (L.platform, "P1"), (L.tenant, "T2"), (L.landing, "LB"), (L.kit, "K2")) for i in range(2)]
    copy = simple("copy", (L.platform, "P1"), (L.tenant, "T9"), (L.landing, "LA"), (L.kit, "K1"))
    return dataset(*a, *b, copy)


def _partition(campaigns):
    return {frozenset(c.capture_ids) for c in campaigns}


def test_one_partition_per_relation_copied_kit_is_template_not_campaign():
    ds = _two_campaigns_and_a_copy()
    be = stub_backend(ds)
    camp, _ = group(be, Relation.campaign)
    tmpl, _ = group(be, Relation.template)
    a = frozenset(f"a{i}-c1" for i in range(3))
    assert a in _partition(camp)
    assert frozenset({"copy-c1"}) in _partition(camp)
    assert a | {"copy-c1"} in _partition(tmpl)
    for relation in (Relation.campaign, Relation.template):
        cs, _ = group(be, relation)
        flat = [c for x in cs for c in x.capture_ids]
        assert sorted(flat) == sorted(set(flat))  # a partition: no capture twice
        assert all(c.relation is relation for c in cs)


def test_campaign_links_are_qualifying_and_inside_the_cluster():
    ds = _two_campaigns_and_a_copy()
    camps, links = group(stub_backend(ds), Relation.campaign)
    by_id = {l.id: l for l in links}
    for c in camps:
        for lid in c.link_ids:
            l = by_id[lid]
            assert {l.a, l.b} <= set(c.capture_ids)
            assert l.score_bits >= TAU
    assert any(len(c.capture_ids) == 3 and c.label.startswith("campaign: 3 incidents") for c in camps)


def test_conflict_is_cannot_link_for_campaign_not_for_template():
    # Same kit and landing (template +7), different tenant: template groups, campaign does not.
    x = simple("x", (L.tenant, "T1"), (L.landing, "L1"), (L.kit, "K1"), (L.offer, "O1"), (L.tracking_account, "PX", True))
    y = simple("y", (L.tenant, "T2"), (L.landing, "L1"), (L.kit, "K1"), (L.offer, "O1"), (L.tracking_account, "PX", True))
    ds = dataset(x, y)
    be = stub_backend(ds)
    assert _partition(group(be, Relation.campaign)[0]) == {frozenset({"x-c1"}), frozenset({"y-c1"})}
    assert _partition(group(be, Relation.template)[0]) == {frozenset({"x-c1", "y-c1"})}


def test_incident_captures_are_must_linked_and_decoy_does_not_pull():
    # Incident m: ad + lander (lander matches campaign A) + decoy (decoy matches unrelated campaign D).
    caps = [cap("m-ad", "ad"), cap("m-l", "lander"), cap("m-d", "decoy")]
    m = incident("m", caps, {"m-ad": [(L.creative, "CR9")], "m-l": [(L.tenant, "T1"), (L.landing, "LA")], "m-d": [(L.kit, "KD"), (L.landing, "LD"), (L.offer, "OD")]})
    a = [simple(f"a{i}", (L.tenant, "T1"), (L.landing, "LA")) for i in range(2)]
    d = [simple(f"d{i}", (L.kit, "KD"), (L.landing, "LD"), (L.offer, "OD")) for i in range(2)]
    ds = dataset(m, *a, *d)
    be = stub_backend(ds)
    part = _partition(group(be, Relation.campaign)[0])
    assert frozenset({"m-ad", "m-l", "a0-c1", "a1-c1"}) in part
    assert frozenset({"d0-c1", "d1-c1"}) in part  # under campaign the decoy clusters only with decoys
    assert not any("m-d" in p for p in part)  # a decoy-only singleton is not a campaign
    tpart = _partition(group(be, Relation.template)[0])
    assert frozenset({"m-d", "d0-c1", "d1-c1"}) in tpart  # under template it joins D on its own evidence


def test_duplicates_follow_their_canonical_capture():
    x = incident("x", [cap("x-c1")], {"x-c1": [(L.tenant, "T1"), (L.landing, "LA")]})
    dup = incident("xd", [cap("xd-c1", duplicate_of="x-c1")], {"xd-c1": [(L.tenant, "T1"), (L.landing, "LA")]})
    y = simple("y", (L.tenant, "T1"), (L.landing, "LA"))
    ds = dataset(x, dup, y)
    part = _partition(group(stub_backend(ds), Relation.campaign)[0])
    assert frozenset({"x-c1", "xd-c1", "y-c1"}) in part


def test_link_dataset_validates_with_unique_ids_across_relations():
    ds = _two_campaigns_and_a_copy()
    out = link_dataset(ds, backend=stub_backend(ds))
    again = Dataset.model_validate(json.loads(out.to_json()))
    assert {c.relation for c in again.campaigns} == set(Relation)
    assert len({l.id for l in again.links}) == len(again.links)
    assert len({c.id for c in again.campaigns}) == len(again.campaigns)
    assert again.links and all(l.evidence for l in again.links)


def test_collector_values_do_not_create_pairs():
    from ftf.contract.models import Indicator, Level, Observed

    x, y = simple("x", (L.kit, "K1")), simple("y", (L.kit, "K2"))
    for inc in (x, y):
        inc.indicators.append(Indicator(id=f"{inc.id}:fp", capture_id=f"{inc.id}-c1", layer=L.collector, kind="aio_fingerprint", level=Level.capture, value="FP-SAME", observed=Observed.live, extractor="synthetic@0"))
    ds = dataset(x, y)
    be = stub_backend(ds)
    group(be, Relation.campaign)
    assert be.score.calls == 0


def test_one_kit_layer_is_enough_for_template_not_for_campaign():
    x = simple("x", (L.kit, "K1"), (L.tenant, "T1"))
    y = simple("y", (L.kit, "K1"), (L.tenant, "T1"))
    ds = dataset(x, y)
    be = stub_backend(ds)
    # template: kit 3 bits, one layer; lower tau so only the layer rule decides.
    be.threshold = lambda relation: 3.0
    assert _partition(group(be, Relation.template)[0]) == {frozenset({"x-c1", "y-c1"})}
    be.threshold = lambda relation: 9.0  # campaign: kit + tenant = 8 < 9 -> apart
    assert _partition(group(be, Relation.campaign)[0]) == {frozenset({"x-c1"}), frozenset({"y-c1"})}
    one = cluster(["x", "y"], [ps("x", "y", 8, layers=1)], TAU, min_layers=1)
    assert one == [{"x", "y"}]


def test_decoy_only_clusters_are_not_campaigns():
    d = [simple(f"d{i}", (L.kit, "KD"), (L.landing, "LD"), (L.offer, "OD"), stage="decoy") for i in range(2)]
    x = simple("x", (L.kit, "K1"))
    ds = dataset(*d, x)
    camps, _ = group(stub_backend(ds), Relation.campaign)
    assert _partition(camps) == {frozenset({"x-c1"})}


def test_non_decoy_duplicate_keeps_a_decoy_canonical_in_the_partition():
    d = incident("d", [cap("d-c1", "decoy")], {"d-c1": [(L.kit, "K1")]})
    dup = incident("e", [cap("e-c1", "lander", duplicate_of="d-c1")], {"e-c1": [(L.kit, "K1")]})
    ds = dataset(d, dup)
    camps, _ = group(stub_backend(ds), Relation.campaign)
    assert _partition(camps) == {frozenset({"d-c1", "e-c1"})}


def test_platform_conflict_is_not_cannot_link():
    x = simple("x", (L.platform, "P1"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"))
    y = simple("y", (L.platform, "P2"), (L.tenant, "T1"), (L.landing, "LA"), (L.kit, "K1"))
    ds = dataset(x, y)
    assert _partition(group(stub_backend(ds), Relation.campaign)[0]) == {frozenset({"x-c1", "y-c1"})}


def test_candidate_pairs_use_normalised_values_and_skip_unknown():
    from ftf.linking.grouping import candidate_pairs

    x = simple("x", (L.landing, "LA  1"), (L.infrastructure, "unknown"))
    y = simple("y", (L.landing, " LA 1"), (L.infrastructure, "unknown"))
    z = simple("z", (L.infrastructure, "unknown"))
    pairs = candidate_pairs(dataset(x, y, z), Relation.template)
    assert pairs == {("x-c1", "y-c1")}


def test_incident_with_a_duplicate_capture_stays_whole():
    x = incident("x", [cap("x-c1")], {"x-c1": [(L.kit, "K1")]})
    y = incident("y", [cap("y-c1", duplicate_of="x-c1"), cap("y-c2", "ad")], {"y-c1": [(L.kit, "K1")], "y-c2": [(L.offer, "O9")]})
    ds = dataset(x, y)
    part = _partition(group(stub_backend(ds), Relation.campaign)[0])
    assert part == {frozenset({"x-c1", "y-c1", "y-c2"})}


def test_duplicate_lander_and_another_lander_are_not_must_linked():
    # y's first lander is a copy of x's capture; its second lander is another site with no shared evidence.
    x = incident("x", [cap("x-c1")], {"x-c1": [(L.kit, "K1")]})
    y = incident("y", [cap("y-c1", duplicate_of="x-c1"), cap("y-c2")], {"y-c1": [(L.kit, "K1")], "y-c2": [(L.offer, "O9")]})
    ds = dataset(x, y)
    part = _partition(group(stub_backend(ds), Relation.campaign)[0])
    assert part == {frozenset({"x-c1", "y-c1"}), frozenset({"y-c2"})}


# --- incident chains --------------------------------------------------------------


def test_incident_with_two_unrelated_landers_is_not_joined():
    # One case folder holding two unrelated sites, each matching its own campaign. No conflict
    # between them, so only the must-link could join them (and, through them, A and B).
    m = incident("m", [cap("m-x"), cap("m-y")], {"m-x": [(L.kit, "K1"), (L.landing, "LA")], "m-y": [(L.kit, "K2"), (L.landing, "LB")]})
    a = [simple(f"a{i}", (L.kit, "K1"), (L.landing, "LA")) for i in range(2)]
    b = [simple(f"b{i}", (L.kit, "K2"), (L.landing, "LB")) for i in range(2)]
    ds = dataset(m, *a, *b)
    be = stub_backend(ds)
    for relation in (Relation.campaign, Relation.operator, Relation.template):
        part = _partition(group(be, relation)[0])
        assert frozenset({"m-x", "a0-c1", "a1-c1"}) in part, relation
        assert frozenset({"m-y", "b0-c1", "b1-c1"}) in part, relation
    alone = dataset(m)
    assert _partition(group(stub_backend(alone), Relation.campaign)[0]) == {frozenset({"m-x"}), frozenset({"m-y"})}


def test_incident_with_ad_and_one_lander_is_joined():
    m = incident("m", [cap("m-ad", "ad"), cap("m-pre", "pre_lander"), cap("m-l")], {"m-ad": [(L.creative, "CR9")], "m-l": [(L.kit, "K1")]})
    ds = dataset(m)
    for relation in (Relation.campaign, Relation.template):
        assert _partition(group(stub_backend(ds), relation)[0]) == {frozenset({"m-ad", "m-pre", "m-l"})}


def test_incident_with_ad_and_two_landers_sharing_a_tenant_is_joined_through_evidence():
    # Ad, two landers on one tenant/owner and a decoy (the case-61 shape).
    caps = [cap("m-ad", "ad"), cap("m-l1"), cap("m-l2"), cap("m-d", "decoy")]
    vals = {
        "m-ad": [(L.creative, "CR1")],
        "m-l1": [(L.creative, "CR1"), (L.tenant, "T1", True), (L.owner, "W1", True), (L.landing, "LA1")],
        "m-l2": [(L.tenant, "T1", True), (L.owner, "W1", True), (L.landing, "LA2")],
        "m-d": [(L.kit, "KD")],
    }
    ds = dataset(incident("m", caps, vals))
    be = stub_backend(ds)
    for relation in (Relation.campaign, Relation.operator):
        assert _partition(group(be, relation)[0]) == {frozenset({"m-ad", "m-l1", "m-l2"})}, relation
    # Without shared evidence the ad stays its own unit, and the landers still join.
    vals["m-ad"] = [(L.creative, "CR-other")]
    ds = dataset(incident("m", caps, vals))
    part = _partition(group(stub_backend(ds), Relation.campaign)[0])
    assert part == {frozenset({"m-l1", "m-l2"}), frozenset({"m-ad"})}


def test_ad_attaches_to_the_lander_it_shares_most_evidence_with():
    caps = [cap("m-ad", "ad"), cap("m-l1"), cap("m-l2")]
    vals = {"m-ad": [(L.creative, "CR1"), (L.offer, "O1")], "m-l1": [(L.creative, "CR1")], "m-l2": [(L.creative, "CR1"), (L.offer, "O1")]}
    ds = dataset(incident("m", caps, vals))
    from ftf.linking.grouping import _pair, must_link_groups

    be = stub_backend(ds)
    by_pair = {_pair(a, b): be.score(a, b, Relation.campaign) for a, b in (("m-ad", "m-l1"), ("m-ad", "m-l2"), ("m-l1", "m-l2"))}
    assert must_link_groups(ds, by_pair) == [["m-l2", "m-ad"]]


def test_unknown_stage_captures_stay_must_linked():
    # Two fetches of one URL (default and victim profile) share nothing but are one chain.
    m = incident("m", [cap("m-c1", "unknown"), cap("m-c2", "unknown")], {"m-c1": [(L.kit, "K1")], "m-c2": [(L.offer, "O9")]})
    assert _partition(group(stub_backend(dataset(m)), Relation.campaign)[0]) == {frozenset({"m-c1", "m-c2"})}


def test_ad_skips_a_lander_it_conflicts_with():
    caps = [cap("m-ad", "ad"), cap("m-l1"), cap("m-l2")]
    vals = {
        "m-ad": [(L.tenant, "T1"), (L.creative, "CR1"), (L.landing, "LX")],
        "m-l1": [(L.tenant, "T2"), (L.creative, "CR1"), (L.landing, "LX")],  # 3 + 4 - 4 = 3 bits, conflict
        "m-l2": [(L.creative, "CR1")],  # 3 bits, clean
    }
    ds = dataset(incident("m", caps, vals))
    from ftf.linking.grouping import _pair, must_link_groups

    be = stub_backend(ds)
    by_pair = {_pair(a, b): be.score(a, b, Relation.campaign) for a, b in (("m-ad", "m-l1"), ("m-ad", "m-l2"))}
    assert must_link_groups(ds, by_pair) == [["m-l2", "m-ad"]]


def test_lander_duplicating_an_unknown_stage_capture_still_counts_as_a_lander():
    f = incident("f", [cap("f-c1", "unknown")], {"f-c1": [(L.kit, "K1")]})
    m = incident("m", [cap("m-x", duplicate_of="f-c1"), cap("m-y")], {"m-x": [(L.kit, "K1")], "m-y": [(L.offer, "O9")]})
    part = _partition(group(stub_backend(dataset(f, m)), Relation.campaign)[0])
    assert part == {frozenset({"f-c1", "m-x"}), frozenset({"m-y"})}


def test_landers_on_the_same_own_host_are_one_site():
    # a live page and its archived copy in one case folder share the scam's own host.
    from ftf.linking.grouping import _pair, must_link_groups

    ds = dataset(incident("m", [cap(f"m-l{i}") for i in range(1, 6)], {f"m-l{i}": [] for i in range(1, 6)}))

    def host(value, status=EvidenceStatus.shared, bits=0.0):  # bits after the within-layer max may be 0
        return EvidenceRow(layer=L.landing, kind="landing_host", status=status, value=value, bits=bits)

    pair = lambda a, b, rows: PairScore(a=a, b=b, relation=Relation.campaign, bits=3.0, rows=rows, layers_positive=1)
    by_pair = {
        _pair("m-l1", "m-l2"): pair("m-l1", "m-l2", [host("a.example", bits=3.0)]),
        _pair("m-l1", "m-l3"): pair("m-l1", "m-l3", [host("hub.example", EvidenceStatus.hub_subsumed)]),
    }
    assert must_link_groups(ds, by_pair) == [["m-l1", "m-l2"]]
    by_pair[_pair("m-l1", "m-l2")] = pair("m-l1", "m-l2", [host("a.example")])  # subsumed by a landing id: still one site
    assert must_link_groups(ds, by_pair) == [["m-l1", "m-l2"]]
    conflict = EvidenceRow(layer=L.tenant, kind="tenant_id", status=EvidenceStatus.conflict, bits=-4.0)
    by_pair[_pair("m-l1", "m-l2")] = pair("m-l1", "m-l2", [host("a.example"), conflict])
    assert must_link_groups(ds, by_pair) == []
    # m-l4 shares one host with m-l3 and another with m-l5: ambiguous, nothing is chained through it
    by_pair[_pair("m-l3", "m-l4")] = pair("m-l3", "m-l4", [host("b.example")])
    by_pair[_pair("m-l4", "m-l5")] = pair("m-l4", "m-l5", [host("c.example")])
    assert must_link_groups(ds, by_pair) == []
