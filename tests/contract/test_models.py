import json

import pytest
from pydantic import ValidationError

from ftf.contract import (
    CONTRACT_VERSION,
    ArtefactType,
    Dataset,
    EvidenceRow,
    Indicator,
    Layer,
    json_schema,
    linkable,
)


def _ind(id, capture_id, layer, kind, value="v", **kw):
    return {
        "id": id,
        "capture_id": capture_id,
        "layer": layer,
        "kind": kind,
        "level": kw.pop("level", "operator"),
        "value": value,
        "observed": "live",
        "extractor": "test@0",
        **kw,
    }


def _doc():
    def incident(n):
        return {
            "id": f"inc{n}",
            "source": "test",
            "captures": [
                {
                    "id": f"inc{n}/ad",
                    "stage": "ad",
                    "artefacts": [{"id": f"inc{n}/ad/shot", "type": "ad_screenshot"}],
                    "missing": [{"modality": "video", "reason": "not provided"}],
                },
                {
                    "id": f"inc{n}/lander",
                    "stage": "lander",
                    "artefacts": [{"id": f"inc{n}/lander/har", "type": "har"}],
                },
            ],
            "indicators": [
                _ind(f"inc{n}/pixel", f"inc{n}/lander", "tracking_account", "meta_pixel", "123",
                     provenance={"artefact_id": f"inc{n}/lander/har"}),
                _ind(f"inc{n}/fp", f"inc{n}/lander", "collector", "aio_fingerprint", "abc", level="capture"),
                {**_ind(f"inc{n}/emb", f"inc{n}/lander", "creative", "text_embedding", None, level="campaign"),
                 "vector_ref": {"store": "vectors/text.f32", "key": f"inc{n}/lander", "model": "e5", "dim": 384}},
            ],
        }

    return {
        "contract_version": CONTRACT_VERSION,
        "name": "unit",
        "incidents": [incident(1), incident(2)],
        "links": [
            {
                "id": "L1",
                "a": "inc1/lander",
                "b": "inc2/lander",
                "relation": "tracking_account",
                "level": "strict",
                "method": "test",
                "evidence": [
                    {"layer": "tracking_account", "kind": "meta_pixel", "status": "shared", "bits": 6.0,
                     "indicator_ids": ["inc1/pixel", "inc2/pixel"]},
                    {"layer": "collector", "kind": "aio_fingerprint", "status": "collector_excluded",
                     "indicator_ids": ["inc1/fp", "inc2/fp"]},
                    {"layer": "landing", "kind": "html", "status": "missing"},
                ],
            }
        ],
        "campaigns": [{"id": "C1", "label": "c", "relation": "tracking_account",
                       "capture_ids": ["inc1/lander", "inc2/lander"], "link_ids": ["L1"]}],
        "assignments": [{"incident_id": "inc2", "candidates": [{"campaign_id": "C1", "score_bits": 6.0}],
                         "decision": "assign", "campaign_id": "C1", "evidence_capacity_bits": 20, "method": "test"}],
    }


def test_valid_document_and_helpers():
    ds = Dataset.model_validate(_doc())
    assert ds.incident_of("inc2/ad").id == "inc2"
    caps = ds.incidents[0].captures
    assert caps[1].modalities == {ArtefactType.har}
    assert caps[0].missing[0].modality.value == "video"
    emb = [i for i in ds.indicators() if i.kind == "text_embedding"][0]
    assert emb.value is None and emb.vector_ref.dim == 384


def test_round_trip():
    ds = Dataset.model_validate(_doc())
    text = ds.to_json()
    assert json.loads(text)["contract_version"] == CONTRACT_VERSION
    again = Dataset.from_json(text)
    assert again == ds
    assert again.to_json() == text


def test_json_schema_exports():
    schema = json_schema()
    assert schema["title"] == "Dataset"
    assert {"Indicator", "Link", "EvidenceRow", "Campaign", "Assignment"} <= set(schema["$defs"])
    assert "collector" in schema["$defs"]["Layer"]["enum"]
    json.dumps(schema)


@pytest.mark.parametrize("version,ok", [(CONTRACT_VERSION, True), ("0.1.9", True), ("0.2.0", False),
                                        ("1.0.0", False), ("0.1", False), ("x.y.z", False)])
def test_version_check(version, ok):
    doc = {**_doc(), "contract_version": version}
    if ok:
        Dataset.model_validate(doc)
    else:
        with pytest.raises(ValidationError, match="contract_version"):
            Dataset.model_validate(doc)


def test_linkable_and_collector_kind_forced_to_collector_layer():
    ds = Dataset.model_validate(_doc())
    by_kind = {i.kind: i for i in ds.indicators()}
    assert linkable(by_kind["meta_pixel"])
    assert not linkable(by_kind["aio_fingerprint"])
    with pytest.raises(ValidationError, match="collector kind"):
        Indicator.model_validate(_ind("x", "c", "tracking_account", "aio_fingerprint"))


def test_collector_evidence_cannot_count():
    with pytest.raises(ValidationError, match="collector_excluded"):
        EvidenceRow(layer=Layer.collector, kind="aio_fingerprint", status="shared", bits=10)
    with pytest.raises(ValidationError, match="collector_excluded"):
        EvidenceRow(layer=Layer.tracking_account, kind="capture_day", status="shared")
    with pytest.raises(ValidationError, match="0 bits"):
        EvidenceRow(layer=Layer.collector, kind="aio_fingerprint", status="collector_excluded", bits=3)


def test_collector_indicator_cannot_back_shared_row():
    doc = _doc()
    # tracking row pointing at a collector indicator: layer mismatch / collector use must fail
    doc["links"][0]["evidence"][0]["indicator_ids"] = ["inc1/fp", "inc2/fp"]
    with pytest.raises(ValidationError, match="collector indicator"):
        Dataset.model_validate(doc)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d["incidents"][0]["indicators"][0].update(value=None), "exactly one"),
        (lambda d: d["incidents"][0]["indicators"][0].update(capture_id="nope"), "not in incident"),
        (lambda d: d["incidents"][0]["captures"][0]["missing"].append(
            {"modality": "ad_screenshot", "reason": "x"}), "both present and missing"),
        (lambda d: d["links"][0].update(b="inc1/lander"), "endpoints must differ"),
        (lambda d: d["links"][0].update(b="ghost"), "unknown capture"),
        (lambda d: d["links"][0].update(relation="friends"), "relation"),
        (lambda d: d["links"][0].update(evidence=[]), "evidence"),
        (lambda d: d["assignments"][0].update(campaign_id=None), "needs campaign_id"),
        (lambda d: d["assignments"][0].update(decision="review"), "only allowed with 'assign'"),
        (lambda d: d["incidents"][1].update(id="inc1"), "duplicate incident"),
        (lambda d: d.update(surprise=1), "surprise"),
    ],
)
def test_invalid_documents(mutate, match):
    doc = _doc()
    mutate(doc)
    with pytest.raises(ValidationError, match=match):
        Dataset.model_validate(doc)
