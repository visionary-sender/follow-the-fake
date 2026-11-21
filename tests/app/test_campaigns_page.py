"""Headless smoke run of the Campaigns page on a synthetic base."""

from pathlib import Path

import pytest

from .synthetic import dataset

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

PAGE = Path(__file__).resolve().parents[2] / "src" / "ftf" / "app" / "pages" / "2_Campaigns.py"


@pytest.fixture
def app(tmp_path, monkeypatch):
    base = tmp_path / "dataset.json"
    base.write_text(dataset().to_json())
    monkeypatch.setenv("FTF_BASE", str(base))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def test_page_renders_campaigns_and_graph(app):
    assert app.title[0].value == "Campaigns"
    assert any("Known campaigns" in h.value for h in app.subheader)
    assert len(app.dataframe) >= 1


def test_page_why_linked_panel(app):
    app.selectbox(key="pair_a").set_value("i2/lander")
    app.selectbox(key="pair_b").set_value("i4/lander")
    app.run()
    assert not app.exception, app.exception
    assert any("Same template" in m.value and "through 1 other" in m.value for m in app.markdown)


def test_page_every_relation_and_group(app):
    for i in range(len(app.selectbox(key="relation").options)):
        app.selectbox(key="relation").select_index(i)
        app.run()
        assert not app.exception, app.exception
        group = app.selectbox(key="group")
        for j in range(1, len(group.options)):
            app.selectbox(key="group").select_index(j)
            app.run()
            assert not app.exception, app.exception
            assert any("Group detail" in h.value for h in app.subheader)
