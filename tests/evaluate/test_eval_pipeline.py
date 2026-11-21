"""Labels, leak check, baseline engine and CLI on a small synthetic dataset (invented values only)."""

import json

import pytest

from ftf.cli import main
from ftf.contract import (
    Artefact,
    Capture,
    Dataset,
    Decision,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
    Relation,
)
from ftf.evaluate.engine import BaselineEngine, get_engine
from ftf.evaluate.labels import build_labels, compare_expected, partition
from ftf.evaluate.leak import HIDDEN_KINDS, MASK, hide_platform_ids, leak_check
from ftf.evaluate.units import GroundTruthSpec, canonical, load_spec, source_keys

TENANT_A = "aaaa0000-1111-4111-8111-000000000001"
TENANT_B = "t-777"
OWNER = "bbbb0000-2222-4222-8222-000000000002"
LANDING = "cccc0000-3333-4333-8333-000000000003"
PLATFORM_ACCOUNT = "dddd0000-4444-4444-8444-000000000004"
HANDLE = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"

A1, A2, A3 = "SETA:1:c1", "SETA:2:alpha.example.test", "SETA:2:beta.example.test"
B1, B1DUP, B2, X = "SETB:p1:c1", "SETB:p2:c1", "SETB:p3:c1", "SETB:p9:c1"


_SEQ = iter(range(10**6))


def ind(cap, kind, value, layer=Layer.kit, level=Level.platform, **prov):
    return Indicator(id=f"{cap}/{kind}/{next(_SEQ)}", capture_id=cap, layer=layer, kind=kind, level=level,
                     value=value, observed=Observed.static, provenance=Provenance(**prov), extractor="test@0")


def make_dataset(with_ids=True):
    def platform(cap, tenant, campaign):
        if not with_ids:
            return []
        return [
            ind(cap, "aio_cdn_tenant", tenant, Layer.tenant, Level.tenant),
            ind(cap, "aio_owner", OWNER, Layer.owner, Level.operator),
            ind(cap, "aio_campaign", campaign, Layer.owner, Level.campaign),
            ind(cap, "aio_handle_key", HANDLE, Layer.tenant, Level.tenant),
        ]

    incidents = [
        Incident(id="SETA:1", source="SETA", source_ref="1", captures=[Capture(id=A1, stage="lander")], indicators=[
            *platform(A1, TENANT_A, "900001"),
            *([ind(A1, "aio_landing", LANDING, Layer.landing, Level.landing)] if with_ids else []),
            ind(A1, "meta_pixel", "1000000000000001", Layer.tracking_account, Level.operator),
            ind(A1, "page_url_shape", f"{HANDLE}={{hex32}}&utm_id={{alnum20}}", Layer.landing),
            ind(A1, "asset_url", f"https://cdn.example.test/landing-cdn/{TENANT_A}/{LANDING}/hero.webp"),
            ind(A1, "asset_url", f"https://cdn.example.test/landing-cdn/{PLATFORM_ACCOUNT}/sdk.js"),
            ind(A1, "aio_action_files", PLATFORM_ACCOUNT, Layer.platform),
            ind(A1, "html_lang", "pl", locator=f"landing-cdn/{TENANT_A}/index.html"),
        ]),
        Incident(id="SETA:2", source="SETA", source_ref="2",
                 captures=[Capture(id=A2, stage="lander"), Capture(id=A3, stage="lander")], indicators=[
            *platform(A2, TENANT_A, "900001"),
            ind(A2, "meta_pixel", "1000000000000001", Layer.tracking_account, Level.operator),
            ind(A3, "xid_tracker_machine", "beef01", Layer.tracker_instance, Level.operator),
            ind(A3, "css_class", "price-t-777"),
            ind(A3, "css_class", "xt-777y"),
        ]),
        Incident(id="SETB:p1", source="SETB", source_ref="p1", captures=[
            Capture(id=B1, stage="lander", artefacts=[Artefact(id=f"{B1}:a", type="html", path=f"p1/{TENANT_B}/page.html")]),
        ], indicators=[*platform(B1, TENANT_B, "900002"), ind(B1, "html_lang", "pl")]),
        Incident(id="SETB:p2", source="SETB", source_ref="p2",
                 captures=[Capture(id=B1DUP, stage="lander", duplicate_of=B1)], indicators=[]),
        Incident(id="SETB:p3", source="SETB", source_ref="p3", captures=[Capture(id=B2, stage="lander")], indicators=[
            ind(B2, "xid_tracker_machine", "beef01", Layer.tracker_instance, Level.operator),
        ]),
        Incident(id="SETB:p9", source="SETB", source_ref="p9", captures=[Capture(id=X, stage="lander")], indicators=[
            ind(X, "html_lang", "en"),
        ]),
    ]
    return Dataset(name="synthetic", incidents=incidents)


SPEC = {
    "schema": "ftf-eval-spec@0.2",
    "analyst_groups": {
        "analyst_operator": [{"members": [A3, B2], "relation": "operator", "reason": "same tracker instance"}],
        "analyst_template": [{"members": [A1, B1, "SETB:p404:c1"], "relation": "template", "reason": "same kit"}],
    },
    "must_not_link": [
        {"a": [A2], "b": [A3], "relation": "campaign", "reason": "two sites in one folder"},
        {"a": [X], "b": ["*"], "relation": "campaign", "reason": "outlier"},
    ],
    "expected_e1": {
        "campaign": [[A1, A2], [B1]],
        "tenant": [[A1, A2], [B1], [A3]],
    },
    "collapse_incidents": [],
}


@pytest.fixture
def ds():
    return make_dataset()


@pytest.fixture
def spec():
    return GroundTruthSpec.from_dict(SPEC)


def test_spec_loading(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC))
    assert load_spec(path).must_not_link[0]["reason"] == "two sites in one folder"
    with pytest.raises(FileNotFoundError):
        load_spec(tmp_path / "absent.json")
    with pytest.raises(ValueError):
        GroundTruthSpec.from_dict({**SPEC, "schema": "other"})
    with pytest.raises(ValueError):
        GroundTruthSpec.from_dict({**SPEC, "surprise": 1})


def test_source_keys(ds):
    assert source_keys(ds)[A3].as_dict() == {"source": "SETA", "folder": "2", "key": "beta.example.test"}


def test_labels(ds, spec):
    labels = build_labels(ds, spec)
    assert sorted(partition(labels, "campaign")) == [[A1, A2], [B1]]
    assert all(B1DUP not in g for lv in ("campaign", "owner", "tenant") for g in partition(labels, lv)), "duplicates collapse"
    assert sorted(partition(labels, "owner")) == [[A1, A2, B1]]
    assert all(g["reason"] and g["source"] in ("platform_id", "analyst_verdict") for g in labels["groups"])
    assert partition(labels, "analyst_template") == [[A1, B1]]
    assert labels["summary"]["missing_captures"] == [
        {"capture": "SETB:p404:c1", "in": "analyst_groups.analyst_template[0]"}]
    outlier = [p for p in labels["must_not_link"] if p["a"] == X]
    assert len(outlier) == 5 and all(p["b"] != B1DUP for p in outlier)
    report = compare_expected(labels, spec, ds)
    assert report == [f"tenant: expected E1 group ['{A3}'] not derived"]
    assert "landing" in labels["summary"]["groups_per_level"] and "template" in labels["summary"]["platform_levels_without_indicators"]
    json.dumps(labels)


def test_hide_and_leak_check(ds):
    hidden, values = hide_platform_ids(ds)
    assert {TENANT_A, TENANT_B, HANDLE, OWNER, LANDING} <= set(values)
    assert not any(i.kind in HIDDEN_KINDS for i in hidden.indicators())
    assert leak_check(hidden, values) == []
    vals = {(i.capture_id, i.kind, i.value) for i in hidden.indicators()}
    by_kind = {(c, k): v for c, k, v in vals}
    assert MASK in by_kind[(A1, "page_url_shape")]
    urls = [v for c, k, v in vals if k == "asset_url"]
    assert urls == [f"https://cdn.example.test/landing-cdn/{PLATFORM_ACCOUNT}/sdk.js"], "only hidden-segment URLs drop"
    assert by_kind[(A1, "aio_action_files")] == PLATFORM_ACCOUNT, "platform constants stay"
    assert {v for c, k, v in vals if k == "css_class"} == {f"price-{MASK}", "xt-777y"}, "short ids match whole tokens"
    # every string field is masked, not only indicator values
    lang = next(i for i in hidden.indicators() if i.capture_id == A1 and i.kind == "html_lang")
    assert lang.provenance.locator == f"landing-cdn/{MASK}/index.html"
    art = next(a for i in hidden.incidents for c in i.captures for a in c.artefacts)
    assert art.path == f"p1/{MASK}/page.html"
    # unhidden data leaks, in values and in other fields, including a uuid's 8-hex prefix
    assert leak_check(ds, values)
    probe = ds.model_copy(deep=True)
    probe.incidents[-1].indicators[0].provenance.note = f"see {TENANT_A[:8]} here"
    hits = leak_check(hide_platform_ids(probe)[0], values)
    assert [(h.path.rsplit(".", 1)[-1], h.indicator_id, h.hidden_value) for h in hits] == [
        ("note", probe.incidents[-1].indicators[0].id, TENANT_A)]


def test_baseline_engine(ds):
    engine = get_engine("baseline")
    out = engine.link_dataset(ds)
    for rel in Relation:
        members = [c.capture_ids for c in out.campaigns if c.relation is rel]
        assert sorted(m for g in members for m in g) == sorted(c.id for i in ds.incidents for c in i.captures)
    camp_of = {m: c.id for c in out.campaigns if c.relation is Relation.campaign for m in c.capture_ids}
    assert camp_of[A3] == camp_of[B2], "shared tracker machine"
    assert camp_of[X] != camp_of[A1]
    new = Incident(id="NEW:1", source="submitted", captures=[Capture(id="NEW:1:c1", stage="lander")],
                   indicators=[ind("NEW:1:c1", "xid_tracker_machine", "beef01", Layer.tracker_instance, Level.operator)])
    res = engine.link_incident(out, new, relation="campaign")
    assert res.assignment.decision is Decision.assign and res.assignment.campaign_id == camp_of[B2]
    assert res.links and res.links[0].a == "NEW:1:c1"
    lonely = new.model_copy(update={"indicators": []})
    assert BaselineEngine().link_incident(out, lonely).assignment.decision is Decision.new


def test_unknown_engine():
    with pytest.raises(ValueError):
        get_engine("nope")


def _write(tmp_path, ds):
    data = tmp_path / "dataset.json"
    data.write_text(ds.to_json())
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC))
    return ["--dataset", str(data), "--spec", str(spec), "--out-dir", str(tmp_path / "out")]


def test_cli_eval_commands(tmp_path, capsys, ds):
    common = _write(tmp_path, ds)
    out = tmp_path / "out"
    assert main(["eval", "labels", *common]) == 0
    assert main(["eval", "leak", *common]) == 0
    assert main(["eval", "baseline", *common]) == 0
    assert main(["eval", "baseline", "--no-hide", *common]) == 0
    labels = json.loads((out / "labels" / "labels.json").read_text())
    assert labels["schema"].startswith("ftf-eval-labels@") and labels["summary"]["e1_discrepancies"]
    leak = json.loads((out / "leak" / "leak.json").read_text())
    assert leak["hits"] == 0 and leak["hidden_values"] > 0
    hidden = json.loads((out / "baseline" / "baseline-hidden.json").read_text())
    visible = json.loads((out / "baseline" / "baseline-visible.json").read_text())
    assert hidden["platform_ids"] == "hidden" and visible["platform_ids"] == "visible"
    row = next(r for r in hidden["levels"] if r["level"] == "campaign")
    assert {"tp", "predicted_pairs", "true_pairs"} <= row["pairwise"].keys()
    assert "pairwise P" in capsys.readouterr().out


@pytest.mark.parametrize("sub", ["labels", "leak", "baseline"])
def test_cli_fails_loudly_without_ground_truth_ids(tmp_path, capsys, sub):
    common = _write(tmp_path, make_dataset(with_ids=False))
    assert main(["eval", sub, *common]) == 2
    assert "none of the ground-truth kinds" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_linking_engine_wraps_ftf_linking(ds):
    engine = get_engine("linking")
    out = engine.link_dataset(ds)
    canon_ids = set(canonical(ds).values())
    for rel in Relation:
        members = [m for c in out.campaigns if c.relation is rel for m in c.capture_ids]
        assert len(members) == len(set(members)), f"{rel.value}: a capture sits in two campaigns"
        assert canon_ids <= set(members), f"{rel.value}: a canonical capture is in no campaign"
    new = Incident(id="NEW:1", source="submitted", captures=[Capture(id="NEW:1:c1", stage="lander")],
                   indicators=[ind("NEW:1:c1", "xid_tracker_machine", "beef01", Layer.tracker_instance, Level.operator)])
    res = engine.link_incident(out, new, relation=Relation.campaign)
    assert res.assignment.incident_id == "NEW:1"
    assert res.assignment.decision in (Decision.assign, Decision.new, Decision.review)
    with pytest.raises(ValueError):
        engine.link_incident(out, ds.incidents[0], relation=Relation.campaign)


def test_default_dataset_is_promoted_build():
    from ftf.evaluate.commands import DEFAULT_DATASET

    assert DEFAULT_DATASET.as_posix().endswith("/ingest/organiser/dataset-indicators.json")


def test_cli_linking_engine_writes_result(tmp_path, capsys, ds):
    common = _write(tmp_path, ds)
    assert main(["eval", "baseline", "--engine", "linking", *common]) == 0
    result = json.loads((tmp_path / "out" / "baseline" / "linking-hidden.json").read_text())
    assert result["engine"] == "linking" and result["platform_ids"] == "hidden"
    assert result["levels"], "one row per label level"
    assert "engine linking, platform ids hidden" in capsys.readouterr().out
