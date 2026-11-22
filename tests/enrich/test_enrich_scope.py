from ftf.enrich.scope import page_urls


def har(*titles, first=None):
    pages = [{"title": t} for t in titles]
    entries = [{"request": {"url": first}}] if first else []
    return {"log": {"pages": pages, "entries": entries}}


def test_page_urls_prefer_page_titles_over_first_request():
    # A video player loads before page document, so page list is the better source.
    data = har("https://door-focus.example/abc", first="https://player.example/embed")
    assert page_urls(data) == ["https://door-focus.example/abc"]


def test_page_urls_fall_back_to_first_request_and_survive_junk():
    assert page_urls(har("Not a URL", first="https://site.example/x")) == ["https://site.example/x"]
    assert page_urls(["not", "a", "har"]) == []
    assert page_urls({"log": {"entries": [None]}}) == []


def test_page_urls_prefer_the_document_request_without_page_titles():
    data = {"log": {"entries": [
        {"request": {"url": "https://tracker.example/pixel"}, "_resourceType": "image"},
        {"request": {"url": "https://landing.example/offer"}, "_resourceType": "document"},
    ]}}
    assert page_urls(data) == ["https://landing.example/offer"]


def test_malformed_har_structure_gives_no_urls():
    assert page_urls({"log": {"pages": "not-a-list", "entries": [{"request": "text"}]}}) == []
    assert page_urls({"log": {"entries": [{"_resourceType": "document", "request": "text"}]}}) == []
