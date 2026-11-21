import json

from ftf.enrich.scope import organiser_domains, page_urls


def har(*titles, first=None):
    pages = [{"title": t} for t in titles]
    entries = [{"request": {"url": first}}] if first else []
    return {"log": {"pages": pages, "entries": entries}}


def test_page_urls_prefer_page_titles_over_first_request():
    # A video player loads before the page document, so the page list is the better source.
    data = har("https://door-focus.example/abc", first="https://player.example/embed")
    assert page_urls(data) == ["https://door-focus.example/abc"]


def test_page_urls_fall_back_to_first_request_and_survive_junk():
    assert page_urls(har("Not a URL", first="https://site.example/x")) == ["https://site.example/x"]
    assert page_urls(["not", "a", "har"]) == []
    assert page_urls({"log": {"entries": [None]}}) == []


def test_organiser_domains_from_har_and_scam_list_only(tmp_path):
    raw = tmp_path / "raw"
    (raw / "BACKUP" / "p1").mkdir(parents=True)
    (raw / "do").mkdir()
    (raw / "BACKUP" / "p1" / "site.example.har").write_text(
        json.dumps(har("https://lander.example.org/?utm=1", first="https://cdn.example-cdn.net/app.js")), encoding="utf-8"
    )
    (raw / "do" / "dup.example.har").write_text(json.dumps(har("https://LANDER.EXAMPLE.org/other")), encoding="utf-8")
    (raw / "do" / "broken.har").write_text("{not json", encoding="utf-8")
    (raw / "scam_links_odszyfrowane.txt").write_text(
        "ZIDENTYFIKOWANY SCAM\r\n\r\nhttps://scamlink.example.com/products/1?from=google\r\nhttps://other.example.net/x.\r\n",
        encoding="utf-8",
    )
    (raw / "notes.txt").write_text("https://ignored-by-name.example/", encoding="utf-8")

    scope = organiser_domains(raw)
    assert scope.domains == ["example.com", "example.net", "example.org"]
    assert "cdn.example-cdn.net" not in scope.domains  # third-party hosts inside a page are out of scope
    assert scope.sources["example.org"] == ["har_page"]
    assert scope.sources["example.com"] == ["scam_link_list"]
    assert [s.endswith("broken.har") for s in scope.skipped] == [True]


def test_page_urls_prefer_the_document_request_without_page_titles():
    data = {"log": {"pages": [], "entries": [
        {"request": {"url": "https://tracker.example/pixel"}, "_resourceType": "image"},
        {"request": {"url": "https://landing.example/offer"}, "_resourceType": "document"},
    ]}}
    assert page_urls(data) == ["https://landing.example/offer"]


def test_malformed_har_structure_is_skipped_not_fatal(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "odd.har").write_text(json.dumps({"log": {"pages": "not-a-list", "entries": [{"request": "text"}]}}), encoding="utf-8")
    (raw / "good.har").write_text(json.dumps(har("https://landing-ok.live/x")), encoding="utf-8")
    scope = organiser_domains(raw)
    assert scope.domains == ["landing-ok.live"]
    assert scope.skipped == []  # a page list that is not a list gives no pages; the entry is not a dict request
