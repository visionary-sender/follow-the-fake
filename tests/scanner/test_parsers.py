"""Parsers against saved, sanitised fixtures (synthetic ids, `.test`/`.example` domains)."""

import json
from datetime import date
from pathlib import Path

import pytest

from ftf.scanner import certpl, google_atc, meta
from ftf.scanner.records import host_of, normalise_domain, unix_to_date, unwrap_redirect

FX = Path(__file__).parent / "fixtures"


def test_meta_graphql_walks_all_documents_and_skips_garbage():
    recs = meta.parse_graphql((FX / "meta_graphql.txt").read_text(), keyword="kw")
    assert [r.ad_id for r in recs] == ["100000000000001", "100000000000002", "100000000000003"]
    a, b, c = recs
    assert a.page_id == "200000000000001" and a.page_name == "Example News Page"
    assert a.body == "Example investment story text"
    assert a.landing_url == "https://www.example.test/path?a=1"  # l.facebook.com unwrapped
    assert a.effective_landing_domain() == "example.test"
    assert a.display_domain == "WWW.EXAMPLE.TEST"
    assert a.creative_urls == ["https://scontent.example.fbcdn.net/v/t39/orig.jpg?sig=1"]
    assert a.collation_id == "900000000000001"
    assert a.platforms == ["FACEBOOK", "INSTAGRAM"]
    assert (a.start_date, a.end_date) == ("2025-11-17", "2025-11-20")
    assert a.keyword == "kw" and a.status == "ELIGIBLE"
    # DCO / video ad: copy and landing come from the first card, preview image is kept.
    assert b.body == "{{product.brand}}"
    assert b.title == "Card title"
    assert b.landing_url == "https://card.example.test/landing"
    assert b.creative_urls == [
        "https://scontent.example.fbcdn.net/v/preview.jpg",
        "https://scontent.example.fbcdn.net/v/card.jpg",
    ]
    assert b.end_date is None and b.status == "TAKEN_DOWN"
    # Non-http link and data: image are dropped; string body and platform accepted.
    assert c.body == "Plain string body"
    assert c.landing_url is None and c.creative_urls == []
    assert c.platforms == ["MESSENGER"] and c.start_date == "2025-11-19"


def test_meta_ssr_reads_only_json_script_blocks():
    recs = meta.parse_ssr_html((FX / "meta_ssr.html").read_text())
    assert [r.ad_id for r in recs] == ["100000000000001"]
    assert recs[0].body == "SSR copy"


def test_meta_search_url_has_country_keyword_and_date():
    url = meta.search_url("pasywny dochód", since="2026-11-14")
    assert url.startswith("https://www.facebook.com/ads/library/?")
    assert "country=PL" in url and "q=pasywny+doch%C3%B3d" in url
    assert "start_date%5Bmin%5D=2026-11-14" in url


def test_meta_collect_degrades_without_playwright(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    res = meta.collect(["a", "b"], since=None)
    assert [r.status for r in res] == ["unavailable", "unavailable"]
    assert "playwright" in res[0].evidence["reason"]


def test_google_suggestions():
    doc = google_atc._loads((FX / "google_suggestions.json").read_bytes())
    s = google_atc.parse_suggestions(doc)
    assert s.advertisers == [("AR00000000000000000001", "Example Invest Ltd"), ("AR00000000000000000002", "Other Trading Sp. z o.o.")]
    # Polish advertisers are ranked first.
    doc["1"].insert(0, doc["1"].pop(1))
    assert google_atc.parse_suggestions(doc).advertisers[0][0] == "AR00000000000000000001"
    assert s.domains == ["invest.example.test"]


def test_google_creatives():
    doc = google_atc._loads((FX / "google_creatives.json").read_bytes())
    recs, token = google_atc.parse_creatives(doc, keyword="invest")
    assert token == "CgoNEXTPAGE"
    assert [r.ad_id for r in recs] == ["CR00000000000000000011", "CR00000000000000000012"]
    a, b = recs
    assert a.advertiser_id == "AR00000000000000000001" and a.advertiser_name == "Example Invest Ltd"
    assert a.landing_url is None and a.effective_landing_domain() == "invest.example.test"
    assert a.creative_urls == ["https://tpc.googlesyndication.com/archive/simgad/1234567890"]
    assert a.display_format == "IMAGE" and a.platforms == ["GOOGLE"]
    assert (a.start_date, a.end_date) == ("2025-11-17", "2025-11-21")
    assert b.creative_urls == [
        "https://displayads-formats.googleusercontent.com/ads/preview/content.js?client=x&obfuscatedCustomerId=1"
    ]
    assert b.display_format == "TEXT"


def test_google_request_filters():
    req = google_atc.creatives_request(advertiser_id="AR1", since=date(2026, 11, 14), until=date(2026, 11, 21), page_size=10)
    assert req["3"]["8"] == [2616] and req["3"]["13"] == {"1": ["AR1"]}
    assert (req["3"]["6"], req["3"]["7"]) == (20261114, 20261121)
    assert req["2"] == 10 and "4" not in req
    req = google_atc.creatives_request(domain="x.test", token="T")
    assert req["3"]["12"] == {"1": "x.test", "2": True} and req["4"] == "T" and "6" not in req["3"]


def test_google_collect_with_fake_client(monkeypatch):
    calls = []

    def fake_post(client, method, req):
        calls.append(method)
        name = "google_suggestions.json" if method.endswith("Suggestions") else "google_creatives.json"
        return google_atc._loads((FX / name).read_bytes())

    monkeypatch.setattr(google_atc, "_post", fake_post)
    res = google_atc.collect(["invest"], since=date(2025, 11, 1), client=None, max_per_keyword=5, max_total=5)
    assert res[0].status == "ok"
    # The September creative ended before `since` and is dropped; duplicates across targets merge.
    assert [r.ad_id for r in res[0].records] == ["CR00000000000000000011"]
    assert calls[0].endswith("SearchSuggestions")


def test_google_collect_reports_block(monkeypatch):
    from ftf.scanner.http import FetchError

    def boom(*a, **k):
        raise FetchError("HTTP 429", 429)

    monkeypatch.setattr(google_atc, "_post", boom)
    res = google_atc.collect(["x", "y"], since=None, client=None)
    assert res[0].status == "blocked" and "429" in res[0].evidence["error"]
    assert res[1].status == "skipped"  # no retry against a source that is rate-limiting us


def test_certpl_filters_since_deleted_and_keywords():
    doc = json.loads((FX / "certpl_domains.json").read_text())
    recs = certpl.parse_list(doc, since=date(2026, 11, 19))
    assert [r.landing_domain for r in recs] == ["olx-example.test", "invest-example.test", "pasywny-dochod.example"]
    assert recs[0].start_date == "2026-11-21" and recs[0].register_id == "5000003"
    assert recs[0].ad_id == "olx-example.test" and recs[0].incident_id == "certpl:olx-example.test"
    kw = certpl.parse_list(doc, since=date(2026, 11, 1), keywords=["Pasywny Dochód", "invest"])
    assert [(r.landing_domain, r.keyword) for r in kw] == [
        ("invest-example.test", "invest"),
        ("pasywny-dochod.example", "Pasywny Dochód"),
    ]
    with pytest.raises(ValueError):
        certpl.parse_list({"not": "a list"}, since=None)


@pytest.mark.parametrize(
    "value,expected",
    [("WWW.Example.TEST.", "example.test"), ("https://Sub.Example.test:8443/x", "sub.example.test"), ("żółw.pl", "żółw.pl".encode("idna").decode()), ("nodot", None), ("", None), (None, None)],
)
def test_normalise_domain(value, expected):
    assert normalise_domain(value) == expected


def test_url_helpers():
    assert unwrap_redirect("https://l.facebook.com/l.php?u=javascript%3Aalert(1)") == "https://l.facebook.com/l.php?u=javascript%3Aalert(1)"
    assert unwrap_redirect("https://example.test/l.php?u=https%3A%2F%2Fx.test") == "https://example.test/l.php?u=https%3A%2F%2Fx.test"
    assert host_of("not a url") is None
    assert unix_to_date("abc") is None and unix_to_date(0) is None and unix_to_date(10**20) is None


def test_google_collect_pages_past_seen_creatives(monkeypatch):
    pages = {
        None: {"1": [{"1": "AR1", "2": "CR1", "14": "a.test"}, {"1": "AR1", "2": "CR2", "14": "a.test"}], "2": "P2"},
        "P2": {"1": [{"1": "AR1", "2": "CR3", "14": "a.test"}]},
    }

    def fake_post(client, method, req):
        if method.endswith("Suggestions"):
            return {"1": [{"1": {"1": "A", "2": "AR1"}}]}
        return pages[req.get("4")]

    monkeypatch.setattr(google_atc, "_post", fake_post)
    skip = {"google_atc:CR1", "google_atc:CR2"}
    res = google_atc.collect(["a"], since=None, client=None, max_per_keyword=1, skip=skip)
    assert [r.ad_id for r in res[0].records] == ["CR3"]
    assert "google_atc:CR3" in skip


def test_meta_graphql_rate_limit_error_is_read():
    body = '{"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL","code":1675004}],"extensions":{"is_final":true}}'
    assert meta.parse_graphql(body) == []
    assert meta.graphql_errors(body) == ["Rate limit exceeded (1675004)"]
    assert meta.graphql_errors((FX / "meta_graphql.txt").read_text()) == []


def test_certpl_keys_by_registrable_domain_newest_listing():
    doc = [
        {"RegisterPositionId": 1, "DomainAddress": "a.scam-example.com", "InsertDate": "2026-11-19T10:00:00", "DeleteDate": None},
        {"RegisterPositionId": 2, "DomainAddress": "b.scam-example.com", "InsertDate": "2026-11-20T10:00:00", "DeleteDate": None},
        {"RegisterPositionId": 3, "DomainAddress": "shop.example.co.uk", "InsertDate": "2026-11-18T10:00:00", "DeleteDate": None},
    ]
    recs = certpl.parse_list(doc, since=None)
    assert [(r.ad_id, r.register_id, r.landing_domain) for r in recs] == [
        ("scam-example.com", "2", "b.scam-example.com"),
        ("example.co.uk", "3", "shop.example.co.uk"),
    ]


def test_ids_do_not_depend_on_time():
    from ftf.scanner.records import AdRecord

    m = AdRecord(source="meta", ad_id="123")
    g = AdRecord(source="google_atc", ad_id="CR0001")
    assert (m.incident_id, m.capture_ref) == ("adlib:123", "adlib:123:ad")
    assert g.incident_id == "adlib:CR0001"
    assert m.folder("20261121T060000Z") == "20261121T060000Z-meta-123"


def test_meta_platform_shims_are_not_landing_pages():
    node = {
        "ad_archive_id": "1",
        "snapshot": {
            "link_url": "http://fb.me/",
            "cards": [{"link_url": "https://l.facebook.com/l.php?u=https%3A%2F%2Freal.example.test%2Fp&h=x"}],
        },
    }
    rec = meta.parse_node(node)
    assert rec.landing_url == "https://real.example.test/p"
    assert rec.declared_link_url == "http://fb.me/"
    only_shim = meta.parse_node({"ad_archive_id": "2", "snapshot": {"link_url": "https://www.facebook.com/somepage"}})
    assert only_shim.landing_url is None and only_shim.declared_link_url == "https://www.facebook.com/somepage"


def test_google_bq_rows_to_records():
    from ftf.scanner import google_bq

    resp = {
        "schema": {"fields": [{"name": n} for n in ("url", "first_shown", "last_shown", "removal_reason", "violation_category", "decision_type")]},
        "rows": [
            {"f": [{"v": "https://adstransparency.google.com/advertiser/AR111/creative/CR222?region=PL"}, {"v": "2026-11-01"}, {"v": "2026-11-20"}, {"v": "R"}, {"v": "MISREPRESENTATION"}, {"v": "X"}]},
            {"f": [{"v": "https://adstransparency.google.com/removed/creative/CR333"}, {"v": "2026-11-01"}, {"v": "2026-11-20"}, {"v": None}, {"v": None}, {"v": None}]},
            {"f": [{"v": "not a page url"}, {"v": None}, {"v": None}, {"v": None}, {"v": None}, {"v": None}]},
        ],
    }
    recs = google_bq.to_records(google_bq.parse_rows(resp))
    assert len(recs) == 2
    r = recs[0]
    assert (recs[1].ad_id, recs[1].advertiser_id) == ("CR333", None)
    assert (r.ad_id, r.advertiser_id, r.start_date, r.end_date) == ("CR222", "AR111", "2026-11-01", "2026-11-20")
    assert r.status == "removed:MISREPRESENTATION/R"
    assert r.key == "google_atc:CR222" and r.incident_id == "adlib:CR222"


def test_google_bq_unavailable_without_project(monkeypatch):
    from ftf.scanner import google_bq

    monkeypatch.delenv("FTF_BQ_PROJECT", raising=False)
    res = google_bq.collect(since=date(2026, 11, 1), client=None, project=None)
    assert res.status == "unavailable"
