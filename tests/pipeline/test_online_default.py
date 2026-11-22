"""Online by default with automatic offline fallback. No network: see the conftest fixture."""

from collections import Counter

import pytest

from ftf.cli import main
from ftf.enrich.probe import ShortTimeoutClient
from ftf.pipeline import AnalysisInput, analyze
from ftf.pipeline import stages
from pipeline_synthetic import synthetic_base, write_base

REAL_CLIENT = ShortTimeoutClient


@pytest.mark.parametrize("origins, offline, label", [
    ({"live": 4}, False, "online"),
    ({"live": 2, "cache": 2}, False, "online"),
    ({"cache": 4}, True, "offline cache"),
    ({"live": 3}, True, "offline cache"),
    ({"stale-cache": 2, "none": 2}, False, "offline cache"),
    ({"live": 2, "none": 2}, False, "mixed"),
    ({"none": 2}, False, "no answer"),
])
def test_enrichment_label(origins, offline, label):
    assert stages.enrichment_mode(Counter(origins), offline) == label


def test_failed_probe_runs_offline_and_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no enrichment cache here
    monkeypatch.setattr("ftf.enrich.probe.network_up", lambda *a, **k: False)
    res = analyze(AnalysisInput(kind="url", text="https://scam.example.com/x"), synthetic_base(),
                  run_dir=tmp_path / "run", offline=False)
    assert res.ok, res.stages
    assert res.enrichment == "offline cache"
    assert res.summary()["enrichment"] == "offline cache"
    assert any("network probe failed" in note for note in res.notes)
    assert all(i.value == "unknown" for i in res.incident.indicators)


def test_dead_network_after_a_good_probe_falls_back_without_stalling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ftf.enrich.probe.network_up", lambda *a, **k: True)
    monkeypatch.setattr("ftf.enrich.probe.ShortTimeoutClient", lambda: REAL_CLIENT(sleep=lambda s: None))
    res = analyze(AnalysisInput(kind="url", text="https://scam.example.com/x"), synthetic_base(),
                  run_dir=tmp_path / "run", offline=False)
    assert res.ok, res.stages
    assert res.enrichment == "no answer"  # no cache entry exists here, and nothing answered
    enrich = next(s for s in res.stages if s.name == "enrichment")
    assert enrich.status == "ok" and "no answer" in enrich.note
    assert not any("network probe failed" in note for note in res.notes)


def test_analyze_is_online_by_default_and_switches_are_explicit(tmp_path, monkeypatch):
    base = write_base(tmp_path)
    seen = {}

    class Stop(Exception):
        pass

    def fake_analyze(inp, base, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr("ftf.pipeline.commands.analyze", fake_analyze)
    cases = [
        ([], {}, False, False),
        (["--online"], {}, False, False),  # no-op alias
        (["--offline"], {}, True, False),
        ([], {"FTF_OFFLINE": "1"}, True, False),
        (["--fetch"], {}, False, True),
        ([], {"FTF_FETCH": "1"}, False, True),
    ]
    for argv, env, offline, fetch in cases:
        for key in ("FTF_OFFLINE", "FTF_FETCH"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(Stop):
            main(["analyze", "s-2", "--base", str(base), *argv])
        assert (seen["offline"], seen["fetch"]) == (offline, fetch), (argv, env)
