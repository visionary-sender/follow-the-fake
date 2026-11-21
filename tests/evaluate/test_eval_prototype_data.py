"""E0/E1 checks on the real prototype signals; skipped when they are absent (e.g. CI without data)."""

import os
from pathlib import Path

import pytest

from ftf.evaluate.labels import build_labels, compare_expected
from ftf.evaluate.leak import hide_platform_ids, leak_check
from ftf.evaluate.prototype import build_dataset

PROTO = Path(os.environ.get("FTF_PROTOTYPE_DIR") or "/data/derived/evaluation/prototype")

pytestmark = pytest.mark.skipif(
    not (PROTO / "signals.json").is_file(),
    reason="prototype signals not present; on the VM: gcloud storage rsync -r "
    "gs://follow-the-fake-data/outputs/evaluation/prototype/ /data/derived/evaluation/prototype/",
)


def test_prototype_units_labels_and_leak():
    ds = build_dataset(PROTO)
    caps = [c for i in ds.incidents for c in i.captures]
    assert len(caps) == 29 and sum(1 for c in caps if c.duplicate_of) == 1
    labels = build_labels(ds)
    assert compare_expected(labels) == []
    assert labels["summary"]["unresolved_nodes"] == []
    hidden, values = hide_platform_ids(ds)
    assert values and leak_check(hidden, values) == []
