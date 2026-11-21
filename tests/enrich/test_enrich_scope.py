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
    (raw / "BACKUP" / "p1" / "site.live.har").write_text(
        json.dumps(har("https://lander.live/?utm=1", first="https://cdn.example-cdn.net/app.js")), encoding="utf-8"
    )
    (raw / "do" / "dup.live.har").write_text(json.dumps(har("https://LANDER.live/other")), encoding="utf-8")
    (raw / "do" / "broken.har").write_text("{not json", encoding="utf-8")
    (raw / "scam_links_odszyfrowane.txt").write_text(
        "ZIDENTYFIKOWANY SCAM\r\n\r\nhttps://caepertss.com/products/1?from=google\r\nhttps://zzvn.info/x.\r\n",
        encoding="utf-8",
    )
    (raw / "notes.txt").write_text("https://ignored-by-name.example/", encoding="utf-8")

    scope = organiser_domains(raw)
    assert scope.domains == ["caepertss.com", "lander.live", "zzvn.info"]
    assert "cdn.example-cdn.net" not in scope.domains  # third-party hosts inside a page are out of scope
    assert scope.sources["lander.live"] == ["har_page"]
    assert scope.sources["caepertss.com"] == ["scam_link_list"]
    assert [s.endswith("broken.har") for s in scope.skipped] == [True]


def test_page_urls_prefer_the_document_request_without_page_titles():
    data = {"log": {"pages": [], "entries": [
        {"request": {"url": "https://tracker.example/pixel"}, "_resourceType": "image"},
        {"request": {"url": "https://landing.example/offer"}, "_resourceType": "document"},
    ]}}
    assert page_urls(data) == ["https://landing.example/offer"]
