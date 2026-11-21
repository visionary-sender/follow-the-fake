"""Verdicts, URL validation, URL lists and capture records (no browser needed)."""

from __future__ import annotations

import json

import pytest

from ftf.contract import ArtefactType, Dataset, Incident, Observed
from ftf.fetch import capture as cap
from ftf.fetch import classify as cl
from ftf.fetch.browser import FetchConfig, fetch_url, validate_url
from ftf.fetch.cli import read_url_list

TEXT = "Inwestycja z gwarantowanym zyskiem, polecana przez znanych ludzi. " * 2


def _page(title="t", body=TEXT):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


@pytest.mark.parametrize(
    "html,status,outcome",
    [
        (_page(), 200, cl.LIVE),
        (_page("Just a moment..."), 403, cl.BOT_WALL),
        (_page(body=TEXT + "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"), 200, cl.BOT_WALL),
        (_page(body="<div class=\"g-recaptcha\"></div>" + TEXT), 200, cl.BOT_WALL),
        (_page("Not found"), 404, cl.HTTP_ERROR),
        (_page(body="This domain is for sale"), 200, cl.DECOY),
        (_page("Welcome to nginx!"), 200, cl.DECOY),
        (_page("Index of /"), 200, cl.DECOY),
        (_page("", ""), 200, cl.EMPTY),
        (_page("", "<img src='x.png'>"), 200, cl.LIVE),
    ],
)
def test_classify_content(html, status, outcome):
    assert cl.classify_content("https://a.example/", "https://a.example/", status, html).outcome == outcome


def test_captcha_on_a_full_page_is_live():
    html = _page(body=TEXT * 8 + "<div class=\"g-recaptcha\"></div><form><input name=phone></form>")
    assert cl.classify_content("https://a.example/", "https://a.example/", 200, html).outcome == cl.LIVE
    assert cl.classify_content("https://a.example/", "https://a.example/", 403, html).outcome == cl.BOT_WALL


def test_budget_maps_to_size_limit():
    assert cl.outcome_from_guard("budget_exceeded") == cl.SIZE_LIMIT
    assert cl.category(cl.SIZE_LIMIT) == "dead"


def test_offsite_well_known_redirect_is_decoy():
    v = cl.classify_content("https://scam.example/x", "https://www.google.com/", 200, _page())
    assert v.outcome == cl.DECOY and "google.com" in v.reason
    # Staying on the well-known site is not a decoy.
    assert cl.classify_content("https://www.facebook.com/a", "https://m.facebook.com/a", 200, _page()).outcome == cl.LIVE


def test_visible_text_ignores_scripts():
    assert cl.visible_text("<script>var a='long text here'</script><p>hi</p>") == "hi"


@pytest.mark.parametrize(
    "msg,outcome",
    [
        ("net::ERR_NAME_NOT_RESOLVED at https://x", cl.DNS_FAILURE),
        ("net::ERR_TUNNEL_CONNECTION_FAILED at https://x", cl.CONNECTION_FAILED),
        ("net::ERR_CERT_AUTHORITY_INVALID", cl.CONNECTION_FAILED),
        ("something odd", cl.NAVIGATION_ERROR),
    ],
)
def test_net_errors(msg, outcome):
    assert cl.outcome_from_net_error(msg) == outcome


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://example.com/a?b=c", True),
        ("http://93.184.216.34/", True),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("data:text/html,hi", False),
        ("chrome://settings", False),
        ("ftp://example.com/", False),
        ("https://", False),
        ("http://metadata.google.internal/", False),
        ("http://localhost:8080/", False),
        ("https://example.com:99999/", False),
    ],
)
def test_validate_url(url, ok):
    assert (validate_url(url) is None) is ok


def test_invalid_url_is_a_result_not_an_error(tmp_path):
    m = fetch_url("file:///etc/passwd", tmp_path)
    assert m["outcome"] == cl.INVALID_URL and m["category"] == "dead"
    inc = Incident.model_validate(m["incident"])
    c = inc.captures[0]
    assert c.observed is Observed.live
    assert {a.type for a in c.artefacts} == {ArtefactType.url}
    assert {x.modality for x in c.missing} == {ArtefactType.har, ArtefactType.html, ArtefactType.screenshot}
    assert all("invalid_url" in x.reason for x in c.missing)
    assert inc.indicators[0].kind == "fetch_outcome" and inc.indicators[0].layer.value == "collector"
    assert (tmp_path / m["capture_id"] / "capture.json").exists()
    assert cap.load_incident(tmp_path / m["capture_id"]).id == inc.id
    ds = Dataset.from_json(cap.write_dataset(tmp_path).read_text())
    assert [i.id for i in ds.incidents] == [inc.id]


def test_capture_id_is_contract_safe():
    cid = cap.new_capture_id("https://zażółć.example/ścieżka?x=1", 1_790_000_000)
    assert cid.startswith("2026") and "xn--" in cid
    Incident.model_validate({"id": f"fetch:{cid}", "source": "live_fetch",
                             "captures": [{"id": f"fetch:{cid}:c1", "stage": "unknown"}]})


def test_read_url_list(tmp_path):
    p = tmp_path / "links.txt"
    p.write_text("ZIDENTYFIKOWANY SCAM\n\nhttps://a.example/x?y=1\n# https://skip.example/\n"
                 "see http://b.example/ and https://a.example/x?y=1\n", encoding="utf-8")
    assert read_url_list(p) == ["https://a.example/x?y=1", "http://b.example/"]
    q = tmp_path / "prose.txt"
    q.write_text('"https://c.example/a", (https://d.example/b). <https://e.example/>\n'
                 "https://en.example/wiki/Foo_(bar) https://f.example/x&gt;\n", encoding="utf-8")
    assert read_url_list(q) == ["https://c.example/a", "https://d.example/b", "https://e.example/",
                                "https://en.example/wiki/Foo_(bar)", "https://f.example/x"]


def test_config_round_trips_through_json():
    c = FetchConfig(allow_networks=["127.0.0.1/32"], allow_ports=[1234])
    from dataclasses import asdict

    assert FetchConfig(**json.loads(json.dumps(asdict(c)))) == c


def test_profiles_and_fbclid():
    from ftf.fetch.browser import add_fbclid, profile_settings

    assert add_fbclid("https://a.example/p").startswith("https://a.example/p?fbclid=IwAR")
    assert "?q=1&fbclid=IwAR" in add_fbclid("https://a.example/p?q=1#top")
    assert add_fbclid("https://a.example/p?q=1#top").endswith("#top")
    default = profile_settings(FetchConfig(), "140")
    assert "Windows" in default["user_agent"] and not default["is_mobile"]
    assert "Accept-Language" not in default["extra_http_headers"]
    assert "Headless" not in default["extra_http_headers"]["sec-ch-ua"]
    mobile = profile_settings(FetchConfig(profile="victim-pl-mobile"), "140")
    assert "Android" in mobile["user_agent"] and "Chrome/140" in mobile["user_agent"]
    assert mobile["is_mobile"] and mobile["has_touch"] and mobile["locale"] == "pl-PL"
    with pytest.raises(ValueError):
        profile_settings(FetchConfig(profile="nope"), "140")


def test_categories():
    assert cl.category(cl.LIVE) == "live"
    assert cl.category(cl.HTTP_ERROR) == "dead"
    assert {cl.category(o) for o in cl.CLOAKED} == {"cloaked"}
    assert {cl.category(o) for o in cl.DEAD} == {"dead"}
