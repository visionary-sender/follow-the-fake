import json
import re

import pytest
from evaluate_support import FakeEngine, dataset, incident

from ftf.contract import ArtefactType, Dataset, Layer, Relation
from ftf.evaluate.openset import run_distractors
from ftf.evaluate.perturb import (
    MODES,
    brand_negatives,
    celebrity_only,
    collector_injection,
    decision_diff,
    degrade,
    hold_out,
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
        assert all(i.layer is Layer.collector or i.provenance.artefact_id in arts for i in inc.indicators)
        for cap in inc.captures:
            reasons = {m.modality: m.reason for m in cap.missing}
            assert reasons[ArtefactType.video] == "not captured"  # pre-existing entry kept
            for t in ("screenshot", "url", "html", "har", "text"):
                t = ArtefactType(t)
                if t not in keep:
                    assert reasons[t] == f"degraded:{mode}"
    assert not out.links and not out.campaigns and not out.assignments


def test_degrade_keeps_collector_indicators_in_every_mode():
    out = degrade(dataset(), "har")
    kinds = {i.kind for i in out.indicators()}
    # viewport is a collector indicator with no artefact provenance: kept, like in every mode.
    assert kinds == {"fb_pixel", "viewport"}


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


def _with_raw(ds: Dataset, edit) -> Dataset:
    """`ds` with `edit` applied to each incident's dict, validated again."""
    raw = ds.model_dump(mode="json")
    for inc in raw["incidents"]:
        edit(inc)
    return Dataset.model_validate(raw)


def test_kit_copy_scrubs_provenance_and_drops_vectors():
    def edit(inc):
        if inc["id"] != "ia1":
            return
        tenant = next(i for i in inc["indicators"] if i["kind"] == "tenant_id")
        tenant["provenance"]["locator"] = "/pages/a1a1a1a1/x"
        tenant["provenance"]["note"] = "tenant a1a1a1a1 seen"
        inc["indicators"].append({
            **tenant, "id": "a1:vec", "kind": "face_embedding", "layer": "creative", "value": None,
            "vector_ref": {"store": "vectors/orig.npy", "key": "orig-key", "model": "m", "dim": 4},
        })

    (new,) = kit_copy(_with_raw(dataset(), edit), "a1", seed=1).incidents
    dumped = json.dumps(new.model_dump(mode="json"))
    for secret in ("a1a1a1a1", "orig.npy", "orig-key"):
        assert secret not in dumped
    assert all(i.vector_ref is None for i in new.indicators)


def _small(*incidents):
    return Dataset.model_validate({"name": "small", "incidents": list(incidents)})


def test_hold_out_removes_duplicates_from_a_mixed_reference_incident():
    # Held-out ia1 is duplicated inside the mixed imix: that duplicate goes, imix keeps x2.
    ds = _small(
        incident("ia1", [("a1", "A")]),
        incident("imix", [("x1", "A"), ("x2", "B")], dup_of={"x1": "a1"}),
    )
    (mixed,) = hold_out(ds, ["ia1"]).incidents
    assert mixed.id == "imix"
    assert [c.id for c in mixed.captures] == ["x2"]
    assert {i.capture_id for i in mixed.indicators} == {"x2"}


def test_hold_out_of_a_mixed_incident_removes_its_duplicates_elsewhere():
    # Held-out imix's capture x1 is duplicated inside the mixed iz: y1 goes, iz keeps z1.
    ds = _small(
        incident("imix", [("x1", "A"), ("x2", "B")]),
        incident("iz", [("z1", "C"), ("y1", "A")], dup_of={"y1": "x1"}),
        incident("ia2", [("a2", "A")]),
    )
    rest = hold_out(ds, ["imix"])
    assert {c.id for i in rest.incidents for c in i.captures} == {"z1", "a2"}
    iz = next(i for i in rest.incidents if i.id == "iz")
    assert {i.capture_id for i in iz.indicators} == {"z1"}


def test_collector_check_does_not_match_an_item_against_its_duplicate():
    # ip's exact duplicate iq sits in the same campaign as ir. The check must hold iq out with
    # ip, so the swapped ip has nothing to be assigned to.
    ds = _small(
        incident("ip", [("p1", "A")]),
        incident("iq", [("p2", "A")], dup_of={"p2": "p1"}),
        incident("ir", [("r1", "A")]),
    )
    (r,) = run_distractors(FakeEngine(), ds, [collector_injection(ds, "p1", "r1", seed=3)])
    assert r.passed and r.decisions["ip"] == "new"


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
