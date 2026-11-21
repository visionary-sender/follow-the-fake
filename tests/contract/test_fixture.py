"""Validates the private fixture; skipped when it has not been pulled (e.g. CI without data)."""

import pytest

from ftf.contract import EvidenceStatus, Layer, linkable
from ftf.contract.fixture import fixture_dir, load_fixture

pytestmark = pytest.mark.skipif(
    not (fixture_dir() / "dataset.json").is_file(),
    reason="fixture not present; pull it: gcloud storage rsync gs://follow-the-fake-data/outputs/fixture/v0 data/fixture/v0",
)


def test_fixture_loads_and_exercises_contract():
    ds = load_fixture()
    assert 3 <= len(ds.incidents) <= 4
    assert any(len(i.captures) >= 3 for i in ds.incidents), "multi-capture incident"
    assert any(c.missing for i in ds.incidents for c in i.captures), "missing modality"
    assert any(i.vector_ref is not None for i in ds.indicators()), "vector by reference"
    assert any(i.layer is Layer.collector for i in ds.indicators()), "collector artefacts"
    shared_tracking = [
        r for l in ds.links for r in l.evidence
        if r.layer is Layer.tracking_account and r.status is EvidenceStatus.shared
    ]
    assert shared_tracking, "a shared tracking indicator links two cases"
    by_id = {i.id: i for i in ds.indicators()}
    for l in ds.links:
        for r in l.evidence:
            if r.status is EvidenceStatus.shared:
                assert all(linkable(by_id[i]) for i in r.indicator_ids)
    assert ds.from_json(ds.to_json()) == ds
