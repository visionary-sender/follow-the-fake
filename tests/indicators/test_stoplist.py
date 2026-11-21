"""Stop-list matching by kind family, and its use in extract_all."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import ftf.indicators as indicators
from ftf.contract import Artefact, Capture, Dataset, Incident, Indicator, Layer, Level, Observed
from ftf.indicators._stoplist import family, is_stopped, lists

JQUERY_371 = "fc9a93dd241f6b045cbff0481cf4e1901becd0e12fb45166a8f17f95823f0b1a"


def ind(kind: str, value: str, layer: Layer = Layer.infrastructure) -> Indicator:
    return Indicator(id="x", capture_id="c:s", layer=layer, kind=kind, level=Level.landing, value=value,
                     observed=Observed.static, extractor="test@0")


@pytest.mark.parametrize(
    "kind,value",
    [
        ("script_host", "cdn.jsdelivr.net"),
        ("third_party_host", "connect.facebook.net"),
        ("asset_domain", "fonts.gstatic.com"),
        ("registered_domain", "on.aws"),
        ("host", "www.gazeta.pl"),
        ("host", "https://code.jquery.com/jquery-3.7.1.min.js"),
        ("script_url", "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"),
        ("font_url", "https://fonts.googleapis.com/css2?family=Oswald"),
        ("script_src_sha256", JQUERY_371),
        ("logo_url", "https://static.im-g.pl/logo.svg"),
        ("asset_sha256", JQUERY_371),
        ("content_hash", "sha256:" + JQUERY_371),
        ("server_ip", "188.114.96.11"),
        ("server_ip", "2606:4700::6810:1"),
        ("script_basename", "jquery-3.7.1.min.js.pobrane"),
        ("asset_filename", "saved_resource.html"),
        ("response_header", "server: cloudflare"),
        ("url_param", "utm_source=ig"),
    ],
)
def test_hits(kind, value):
    assert is_stopped(ind(kind, value))


@pytest.mark.parametrize(
    "kind,value",
    [
        ("script_host", "tracker.example.test"),
        ("gateway_host", "xd-0000.ecs.us-east-1.on.aws"),  # protected kind
        ("third_party_host", "xd-0000.ecs.us-east-1.on.aws"),  # tenant host under a cloud suffix
        ("registered_domain", "example-scam.test"),
        ("asset_sha256", "0" * 64),
        ("server_ip", "203.0.113.5"),
        ("script_basename", "main.js"),
        ("url", "https://www.facebook.com/some-scam-page"),  # SDK host is not stopped on URL kinds
        ("meta_pixel_id", "1234567890123456"),
        ("fb_pixel", "1234567890123456"),
        ("tenant_id", "666"),
        ("pixel_host", "connect.facebook.net"),
        ("asset_phash", JQUERY_371),
        ("page_title", "jquery-3.7.1.min.js"),  # no family
        ("script_url", "https://cdn.jsdelivr.net/gh/someone/kit@1/app.js"),  # own code on a public CDN
        ("script_url", "https://unpkg.com/some-kit@1.0.0/dist/index.js"),
    ],
)
def test_non_hits(kind, value):
    assert not is_stopped(ind(kind, value))


def test_collector_layer_never_stopped():
    assert not is_stopped(ind("user_agent", "cdn.jsdelivr.net", layer=Layer.collector))


def test_family_rules():
    assert family("landing_host") == "host"
    assert family("landing_registered_domain") == "host"
    assert family("kit_asset_sha256") == "hash"
    assert family("meta_pixel_id") is None
    assert family("tenant_uuid") is None


def test_data_file_loads():
    L = lists()
    assert JQUERY_371 in L.hashes and "fonts.gstatic.com" in L.all_hosts


def test_extract_all_drops_stopped(tmp_path: Path, monkeypatch):
    mod = types.ModuleType("ftf.indicators.zz_fake")

    def extract(incident, capture, root):
        yield ind("script_host", "cdn.jsdelivr.net").model_copy(update={"capture_id": capture.id})
        yield ind("script_host", "tracker.example.test").model_copy(update={"capture_id": capture.id})
        yield ind("meta_pixel_id", "1234567890123456", layer=Layer.tracking_account).model_copy(update={"capture_id": capture.id})

    mod.extract = extract
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setattr(indicators, "extractors", lambda: [mod])
    ds = Dataset(name="t", incidents=[Incident(id="i1", source="t", captures=[
        Capture(id="i1:s1", stage="lander", artefacts=[Artefact(id="i1:a", type="url")])])])
    out = indicators.extract_all(ds, tmp_path)
    values = {i.value for i in out.indicators()}
    assert values == {"tracker.example.test", "1234567890123456"}
