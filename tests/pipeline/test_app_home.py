"""Headless smoke runs of the Streamlit page (AppTest)."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from ftf.contract.fixture import fixture_dir  # noqa: E402
from pipeline_synthetic import write_base  # noqa: E402

HOME = str(Path(__file__).resolve().parents[2] / "src" / "ftf" / "app" / "Home.py")


def _run(at: AppTest) -> AppTest:
    at.run(timeout=60)
    assert not at.exception, at.exception
    return at


def test_home_on_synthetic_base(tmp_path, monkeypatch):
    monkeypatch.setenv("FTF_BASE", str(write_base(tmp_path)))
    monkeypatch.setenv("FTF_RUN_DIR", str(tmp_path / "runs"))
    at = _run(AppTest.from_file(HOME, default_timeout=60))
    at.selectbox(key="item").select("s-2")
    at.button(key="analyze").click()
    _run(at)
    headers = [h.value for h in at.header]
    assert "5. Evidence ledger" in headers and "6. Similar incidents" in headers
    assert any(m.value == "Assign to campaign" for m in at.metric)
    assert at.expander, "ledger expanders rendered"
    # keyword input path: review, no crash
    at.radio(key="kind").set_value("keyword")
    _run(at)
    at.text_input(key="keyword").input("free money")
    at.button(key="analyze").click()
    _run(at)
    assert any(m.value == "Needs review" for m in at.metric)
    assert not at.markdown or all("http" not in m.value for m in at.markdown)


@pytest.mark.skipif(not (fixture_dir() / "dataset.json").is_file(), reason="fixture not present")
def test_home_on_fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("FTF_BASE", raising=False)
    monkeypatch.setenv("FTF_RUN_DIR", str(tmp_path / "runs"))
    at = _run(AppTest.from_file(HOME, default_timeout=60))
    for item in at.selectbox(key="item").options:
        at.selectbox(key="item").select(item)
        at.button(key="analyze").click()
        _run(at)
        assert at.metric
