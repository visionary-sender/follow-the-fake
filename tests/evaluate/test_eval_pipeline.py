"""Units, prototype adapter, labels, leak check, baseline engine and CLI on synthetic data."""

import json

import pytest
from eval_support import HANDLE, PLATFORM, T1, T2, write_prototype

from ftf.cli import main
from ftf.contract import Capture, Dataset, Decision, Incident, Indicator, Layer, Level, Observed, Relation
from ftf.evaluate.engine import BaselineEngine, get_engine
from ftf.evaluate.labels import build_labels, partition
from ftf.evaluate.leak import HIDDEN_KINDS, MASK, hide_platform_ids, leak_check
from ftf.evaluate.prototype import KIND_TABLE, build_dataset
from ftf.evaluate.units import node_source_key, node_to_capture, sanitise


@pytest.fixture
def ds(tmp_path):
    return build_dataset(write_prototype(tmp_path / "proto"))


def test_units_follow_ingest_id_scheme():
    assert node_to_capture("1") == "do_udostepnienia:1:c1"
    assert node_to_capture("p15") == "BACKUP:p15:c1"
    assert node_to_capture("61/s1") == "do_udostepnienia:61:strona_1"
    assert node_to_capture("61/ad") == "do_udostepnienia:61:reklama_1"
    assert node_to_capture("2/healthlinkq") == "do_udostepnienia:2:healthlinkq.pro"
    assert node_source_key("2/acrosscommunities").as_dict() == {
        "source": "do_udostepnienia", "folder": "2", "key": "acrosscommunities.live"}
    assert sanitise("do_udostępnienia / x") == "do_udostepnienia-x"


def test_collector_kinds_map_to_collector_layer():
    for sub in ("capture_day", "capture_time", "screenshot_date", "saved_from"):
        assert KIND_TABLE[sub][1] is Layer.collector


def test_prototype_dataset(ds):
    caps = {c.id: c for i in ds.incidents for c in i.captures}
    assert caps["BACKUP:p2:c1"].duplicate_of == "BACKUP:p1:c1"
    assert caps["do_udostepnienia:61:reklama_1"].stage.value == "ad"
    inc = {i.id: i for i in ds.incidents}
    assert inc["BACKUP:p1"].source == "BACKUP" and inc["BACKUP:p1"].source_ref == "p1"
    tenants = {i.value for i in ds.indicators() if i.kind == "aio_cdn_tenant"}
    assert PLATFORM not in tenants and {T1, T2, "666"} <= tenants
    camps = {(i.capture_id, i.value) for i in ds.indicators() if i.kind == "aio_campaign"}
    assert ("do_udostepnienia:1:c1", "214711") in camps and ("BACKUP:p1:c1", "214711") in camps
    assert not any("EAAsecret" in (i.value or "") for i in ds.indicators())
    assert all(i.layer is Layer.collector for i in ds.indicators() if i.kind in ("capture_day", "local_path"))
    assert Dataset.from_json(ds.to_json()) == ds


def test_labels(ds):
    labels = build_labels(ds)
    camp = sorted(partition(labels, "campaign"))
    assert ["BACKUP:p1:c1", "do_udostepnienia:1:c1"] in camp
    assert all("BACKUP:p2:c1" not in g for g in camp), "duplicates are not separate units"
    assert all(g["reason"] and g["source"] in ("platform_id", "analyst_verdict") for g in labels["groups"])
    p14 = [p for p in labels["must_not_link"] if p["a"] == "BACKUP:p14:c1"]
    assert p14 and all(p["reason"] for p in p14)
    json.dumps(labels)


def test_hide_and_leak_check(ds):
    hidden, values = hide_platform_ids(ds)
    assert {T1, T2, HANDLE, "666"} <= set(values)
    assert not any(i.kind in HIDDEN_KINDS for i in hidden.indicators())
    assert leak_check(hidden, values) == []
    by_kind = {(i.capture_id, i.kind): i.value for i in hidden.indicators()}
    assert MASK in by_kind[("do_udostepnienia:1:c1", "param_shape")]
    assert ("BACKUP:p6:c1", "html_comment") not in by_kind, "asset URL with landing-cdn path dropped"
    assert by_kind[("do_udostepnienia:1:c1", "aio_action_files")] == PLATFORM, "platform constants stay"
    css = {i.value for i in hidden.indicators() if i.kind == "css_class"}
    assert css == {"x666y", f"price-{MASK}"}, "short ids match whole tokens only"
    # unhidden data leaks, including a uuid's 8-hex prefix
    assert leak_check(ds, values)
    probe = ds.model_copy(deep=True)
    probe.incidents[0].indicators.append(Indicator(
        id="probe", capture_id=probe.incidents[0].captures[0].id, layer=Layer.kit, kind="html_comment",
        level=Level.platform, value=f"see {T1[:8]} here", observed=Observed.static, extractor="t@0"))
    hits = leak_check(hide_platform_ids(probe)[0], values)
    assert [h.indicator_id for h in hits] == ["probe"]


def test_baseline_engine(ds):
    engine = get_engine("baseline")
    out = engine.link_dataset(ds)
    for rel in Relation:
        members = [c.capture_ids for c in out.campaigns if c.relation is rel]
        assert sorted(m for g in members for m in g) == sorted(c.id for i in ds.incidents for c in i.captures)
    camp_of = {m: c.id for c in out.campaigns if c.relation is Relation.campaign for m in c.capture_ids}
    assert camp_of["BACKUP:p1:c1"] == camp_of["BACKUP:p6:c1"], "shared html_lang blobs them (loose baseline)"
    assert camp_of["BACKUP:p14:c1"] != camp_of["BACKUP:p1:c1"]
    new = Incident(id="input:abc", source="submitted", captures=[Capture(id="input:abc:c1", stage="lander")],
                   indicators=[Indicator(id="input:abc:c1/meta_pixel/0", capture_id="input:abc:c1",
                                         layer=Layer.tracking_account, kind="meta_pixel", level=Level.operator,
                                         value="111", observed=Observed.static, extractor="t@0")])
    res = engine.link_incident(out, new)
    assert res.assignment.decision is Decision.assign and res.assignment.campaign_id == camp_of["BACKUP:p1:c1"]
    assert res.links and res.links[0].a == "input:abc:c1"
    lonely = new.model_copy(update={"indicators": []})
    assert BaselineEngine().link_incident(out, lonely).assignment.decision is Decision.new


def test_unknown_engine():
    with pytest.raises(ValueError):
        get_engine("nope")


def test_cli_eval_commands(tmp_path, capsys):
    proto = write_prototype(tmp_path / "proto")
    out = tmp_path / "out"
    common = ["--prototype-dir", str(proto), "--out-dir", str(out)]
    assert main(["eval", "prototype", *common]) == 0
    assert main(["eval", "labels", *common]) == 0
    assert main(["eval", "leak", "--dataset", str(out / "prototype-contract" / "dataset.json"), *common]) == 0
    assert main(["eval", "baseline", *common]) == 0
    labels = json.loads((out / "labels" / "labels.json").read_text())
    assert labels["schema"].startswith("ftf-eval-labels@")
    leak = json.loads((out / "leak" / "leak.json").read_text())
    assert leak["hits"] == 0 and leak["hidden_values"] > 0
    base = json.loads((out / "baseline" / "baseline.json").read_text())
    row = next(r for r in base["levels"] if r["level"] == "campaign")
    assert {"tp", "predicted_pairs", "true_pairs"} <= row["pairwise"].keys()
    assert "pairwise P" in capsys.readouterr().out
