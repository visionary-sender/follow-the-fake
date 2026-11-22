import re

import pytest
from evaluate_support import FakeEngine, dataset

from ftf.contract import ArtefactType, Dataset, Layer, Relation
from ftf.evaluate.openset import run_distractors
from ftf.evaluate.perturb import (
    MODES,
    brand_negatives,
    celebrity_only,
    collector_injection,
    decision_diff,
    degrade,
    kit_copy,
    natural_must_not_link,
    platform_only,
    read_corpus_tags,
    with_incidents,
)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_degrade_keeps_only_mode(mode):
    ds = dataset()
    out = Dataset.from_json(degrade(ds, mode).to_json())  # validates against the contract
    keep = MODES[mode]
    for inc in out.incidents:
        arts = {a.id: a for c in inc.captures for a in c.artefacts}
        assert all(a.type in keep for a in arts.values())
        assert all(i.provenance.artefact_id in arts for i in inc.indicators)
        for cap in inc.captures:
            reasons = {m.modality: m.reason for m in cap.missing}
            assert reasons[ArtefactType.video] == "not captured"  # pre-existing entry kept
            for t in ("screenshot", "url", "html", "har", "text"):
                t = ArtefactType(t)
                if t not in keep:
                    assert reasons[t] == f"degraded:{mode}"
    assert not out.links and not out.campaigns and not out.assignments


def test_degrade_drops_unattributed_indicators():
    out = degrade(dataset(), "har")
    kinds = {i.kind for i in out.indicators()}
    assert kinds == {"fb_pixel"}  # viewport has no artefact provenance, so it goes


def test_degrade_unknown_mode():
    with pytest.raises(KeyError):
        degrade(dataset(), "video")


def test_kit_copy_scrubs_identity_and_keeps_template():
    ds = dataset()
    d = kit_copy(ds, "a1", seed=1)
    (new,) = d.incidents
    original = [i for i in ds.incident_of("a1").indicators]
    for secret in ("a1a1a1a1", "5100000000001", "lpAx123456"):
        assert all(secret not in (i.value or "") for i in new.indicators)
    kit = {i.value for i in new.indicators if i.layer is Layer.kit}
    assert kit == {i.value for i in original if i.layer is Layer.kit}
    tenant = next(i.value for i in new.indicators if i.kind == "tenant_id")
    landing = next(i.value for i in new.indicators if i.kind == "landing_path")
    assert re.fullmatch(r"[0-9a-f]{8}", tenant) and tenant in landing  # shape and consistency kept
    assert all(a.path is None and a.sha256 is None for c in new.captures for a in c.artefacts)
    with_incidents(ds, add=[new])  # validates with the reference
    assert d.incidents == kit_copy(ds, "a1", seed=1).incidents  # deterministic

    (r,) = run_distractors(FakeEngine(), ds, [d])
    assert r.passed, r.violations
    assert r.expected_met is True
    assert r.decisions == {new.id: "new"}


def test_platform_only_passes_and_flags_a_leaky_engine():
    ds = dataset()
    d = platform_only(ds, "b1")
    (new,) = d.incidents
    assert {i.layer for i in new.indicators} <= {Layer.platform, Layer.kit, Layer.creative}  # face is ecosystem-level here
    assert not any(i.layer in (Layer.tenant, Layer.tracking_account, Layer.landing) for i in new.indicators)
    assert {m.reason for c in new.captures for m in c.missing} >= {"distractor:platform_only"}
    (r,) = run_distractors(FakeEngine(), ds, [d])
    assert r.passed, r.violations
    (bad,) = run_distractors(FakeEngine(link_platform=True), ds, [d])
    assert any("template" in v for v in bad.violations)


def test_collector_injection_changes_nothing_for_a_correct_engine():
    ds = dataset()
    d = collector_injection(ds, "a1", "c1", seed=3)
    assert set(d.replaces) == {"ia1", "ic1"}
    a, c = d.incidents
    inj = lambda inc: {(i.kind, i.value) for i in inc.indicators if i.id.endswith(tuple(f"inj:{k}" for k in
                       ("browser_fingerprint", "analyst_ip", "fbclid_session", "capture_day")))}
    assert inj(a) == inj(c) and len(inj(a)) == 4
    assert all(i.layer is Layer.collector for inc in d.incidents for i in inc.indicators if ":inj:" in i.id)
    (r,) = run_distractors(FakeEngine(), ds, [d])
    assert r.passed, r.violations


def test_collector_injection_catches_an_engine_that_links_on_collector_fields():
    ds = dataset()
    d = collector_injection(ds, "a1", "c1")
    (r,) = run_distractors(FakeEngine(use_collector=True), ds, [d])
    assert not r.passed
    assert any("link added" in v for v in r.violations)


def test_collector_injection_needs_two_incidents():
    ds = Dataset.model_validate({"name": "x", "incidents": [dataset().incidents[6].model_dump()]})
    with pytest.raises(ValueError):
        collector_injection(ds, "mix/a", "mix/b")


def test_brand_negatives_selected_by_corpus_tag(tmp_path):
    ds = dataset()
    corpus = Dataset.model_validate({
        "name": "corpus", "incidents": [ds.incidents[5].model_dump(), ds.incidents[0].model_dump()],  # ic1, ia1
    })
    tags_file = tmp_path / "corpus_kind.jsonl"
    tags_file.write_text(
        '{"incident_id": "ic1", "corpus_kind": "must_not_link"}\n'
        '{"incident_id": "ia1", "corpus_kind": "must_not_link_blocked"}\n',
        encoding="utf-8",
    )
    tags = read_corpus_tags(tags_file)
    out = brand_negatives(corpus, tags)
    assert [d.name for d in out] == ["brand_negative:ic1"]  # the blocked fetch is not a negative
    assert out[0].subjects == {"brandneg:c1"}
    assert [d.name for d in brand_negatives(corpus, tags, kinds=["must_not_link_blocked"])] == ["brand_negative:ia1"]
    assert brand_negatives(corpus, {}) == []
    # A brand page that carries tenant C of the reference must be caught.
    (r,) = run_distractors(FakeEngine(), ds, out)
    assert not r.passed


def test_celebrity_only_and_face_linking_engine():
    ds = dataset()
    d = celebrity_only(ds, "a1")
    (new,) = d.incidents
    assert {(i.layer, i.kind) for i in new.indicators} == {(Layer.creative, "face_image")}
    (ok,) = run_distractors(FakeEngine(), ds, [d])
    assert ok.passed and ok.decisions == {new.id: "new"}
    (bad,) = run_distractors(FakeEngine(link_faces=True), ds, [d])
    assert any("assigned" in v for v in bad.violations)


def test_celebrity_only_requires_a_face():
    ds = degrade(dataset(), "har")
    with pytest.raises(ValueError):
        celebrity_only(ds, "a1")


def test_natural_must_not_link():
    ds = dataset()
    ok, bad = natural_must_not_link([(["a1"], ["b1"]), (["a1"], ["a2"])])
    r_ok, r_bad = run_distractors(FakeEngine(), ds, [ok, bad])
    assert r_ok.passed
    assert not r_bad.passed  # a1 and a2 do share a campaign
    (tmpl,) = natural_must_not_link([(["a1"], ["b1"])], relations=[Relation.template])
    (r,) = run_distractors(FakeEngine(), ds, [tmpl])
    assert not r.passed  # everyone shares the kit


def test_decision_diff_ignores_campaign_ids():
    e = FakeEngine()
    ds = dataset()
    a = e.link_dataset(ds)
    b = a.model_copy(update={"campaigns": [c.model_copy(update={"id": c.id + "x"}) for c in a.campaigns]})
    assert decision_diff(a, b) == []
    assert decision_diff(a, a.model_copy(update={"links": a.links[1:]}))
