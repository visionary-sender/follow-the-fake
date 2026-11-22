import json

import pytest

from ftf import egress
from ftf.contract import Incident, linkable
from ftf.fetch.browser import fetch_url

GOOD = {"name": "ftf-egress-ec2-a1", "region": "europe-central2", "zone": "europe-central2-a",
        "ip_hash": "sha256:" + "0123456789abcdef" * 2}


def _write(tmp_path, data):
    p = tmp_path / "egress.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def test_current_reads_a_valid_record(tmp_path):
    assert egress.current(_write(tmp_path, GOOD)) == GOOD


def test_extra_keys_are_dropped(tmp_path):
    assert egress.current(_write(tmp_path, {**GOOD, "ip": "203.0.113.7"})) == GOOD


@pytest.mark.parametrize("bad", [
    "not json", "[]", {**GOOD, "ip_hash": "203.0.113.7"}, {**GOOD, "zone": None},
    {**GOOD, "name": "Bad Name"}, {k: v for k, v in GOOD.items() if k != "region"},
])
def test_malformed_records_are_ignored(tmp_path, bad):
    assert egress.current(_write(tmp_path, bad)) is None


def test_missing_file_means_no_egress(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_EGRESS_FILE", str(tmp_path / "absent.json"))
    assert egress.current() is None


def test_manifest_without_egress_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_EGRESS_FILE", str(tmp_path / "absent.json"))
    m = fetch_url("file:///etc/passwd", tmp_path / "out")
    assert m["egress"] is None
    assert [i.kind for i in Incident.model_validate(m["incident"]).indicators] == ["fetch_outcome"]


def test_manifest_records_egress_as_collector_data(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_EGRESS_FILE", str(_write(tmp_path, GOOD)))
    m = fetch_url("file:///etc/passwd", tmp_path / "out")
    assert m["egress"] == GOOD
    inc = Incident.model_validate(m["incident"])
    ind = [i for i in inc.indicators if i.kind == "analyst_ip"]
    assert len(ind) == 1 and ind[0].layer.value == "collector" and not linkable(ind[0])
    assert ind[0].value == "europe-central2-a ip " + GOOD["ip_hash"]
    assert "203.0.113" not in json.dumps(m)
