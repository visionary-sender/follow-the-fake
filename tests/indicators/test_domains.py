"""Own host, domain and redirect-chain indicators over synthetic captures (reserved example domains only)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Capture, Dataset, Incident, Layer, Level, Observed, Stage
from ftf.enrich.normalise import registrable_domain
from ftf.indicators import _pages, domains, extract_all, kit

TOKEN = "EAABsbCS1iHgBAKZBZCxyz0123456789"
LANDING_URL = f"https://www.example.com/land/?fbclid=IwAR0abc&access_token={TOKEN}&clickid=c0ffee"


def doc(url: str, status: int = 200, location: str | None = None, *, pageref: str = "p0", rtype: str | None = "document") -> dict:
    resp = {"status": status, "headers": [], "content": {"mimeType": "text/html", "text": "<html><body>x</body></html>"}}
    if location:
        resp["headers"] = [{"name": "Location", "value": location}]
        resp["content"] = {"mimeType": "text/html", "text": ""}
    e = {"pageref": pageref, "request": {"method": "GET", "url": url}, "response": resp}
    if rtype:
        e["_resourceType"] = rtype
    return e


def chain_har() -> dict:
    return {
        "log": {
            "pages": [{"id": "p0", "title": "https://l.facebook.com/l.php?u=x"}],
            "entries": [
                doc("https://l.facebook.com/l.php?u=x", 302, f"https://trk.example.net/click?clickid=c0ffee&access_token={TOKEN}"),
                doc("https://collect.example.edu/pixel.gif", rtype="image"),
                doc(f"https://trk.example.net/click?clickid=c0ffee&access_token={TOKEN}", 302, "https://cloak.example.org/p/1"),
                doc("https://cloak.example.org/p/1", 307, LANDING_URL),
                doc(LANDING_URL),
                doc("https://ads.example.edu/frame.html"),  # a later document (iframe), not a hop
            ],
        }
    }


def write(root: Path, rel: str, data: str | bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return rel


def cap(cid: str, *arts: Artefact) -> Capture:
    return Capture(id=cid, stage=Stage.lander, artefacts=list(arts))


def run(inc: Incident, root: Path) -> list:
    return [i for c in inc.captures for i in domains.extract(inc, c, root)]


def by_kind(inds) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for i in inds:
        out[i.kind].add(i.value)
    return out


def no_secrets(inds) -> None:
    for i in inds:
        assert "?" not in i.value and "=" not in i.value
        assert TOKEN not in i.value and "fbclid" not in i.value and "c0ffee" not in i.value
        assert "://" not in i.value


# --------------------------------------------------------------------------- HAR chain


def test_har_redirect_chain(tmp_path):
    c = cap("h:c1", Artefact(id="h:har", type=ArtefactType.har, path=write(tmp_path, "h/page.har", json.dumps(chain_har()))))
    inds = run(Incident(id="h", source="synthetic", captures=[c]), tmp_path)
    k = by_kind(inds)
    assert k["landing_host"] == {"www.example.com"}
    assert k["landing_domain"] == {"example.com"}
    assert k["chain_host"] == {"l.facebook.com", "trk.example.net", "cloak.example.org"}
    assert k["chain_domain"] == {"facebook.com", "example.net", "example.org"}
    assert k["redirect_chain_shape"] == {"start:shared>302:tracker>302:hop>307:landing"}
    assert "ads.example.edu" not in k["landing_host"] | k["chain_host"]
    layers = {(i.kind, i.value): (i.layer, i.level) for i in inds}
    assert layers[("chain_host", "trk.example.net")] == (Layer.tracker_instance, Level.operator)
    assert layers[("chain_domain", "example.org")] == (Layer.infrastructure, Level.campaign)
    assert layers[("chain_host", "l.facebook.com")] == (Layer.infrastructure, Level.campaign)
    assert all(i.observed is Observed.live for i in inds)
    shape = next(i for i in inds if i.kind == "redirect_chain_shape")
    assert shape.provenance.note == "shared=facebook.com"
    assert all(i.provenance.artefact_id == "h:har" for i in inds)
    no_secrets(inds)


def test_shared_redirector_never_reaches_dataset(tmp_path):
    c = cap("h:c1", Artefact(id="h:har", type=ArtefactType.har, path=write(tmp_path, "h/page.har", json.dumps(chain_har()))))
    ds = extract_all(Dataset(name="s", incidents=[Incident(id="h", source="synthetic", captures=[c])]), tmp_path)
    values = {i.value for i in ds.indicators() if i.extractor.startswith("domains@")}
    assert "l.facebook.com" not in values and "facebook.com" not in values
    assert {"example.com", "example.net", "example.org"} <= values
    roles = domains.domain_roles(ds)
    assert roles["facebook.com"] == {"shared"}
    assert roles["example.com"] == {"landing"}
    assert roles["example.org"] == {"redirect_hop"}
    assert domains.registered_domains(ds) == ["example.com", "example.net", "example.org"]
    assert domains.registered_domains(ds, include_shared=True) == ["example.com", "example.net", "example.org", "facebook.com"]


def test_har_without_resource_type_and_multi_page_navigation(tmp_path):
    har = {
        "log": {
            "pages": [{"id": "p0", "title": "https://pre.example.org/a"}, {"id": "p1", "title": "https://www.example.com/b"}],
            "entries": [
                doc("https://pre.example.org/a", rtype=None),
                doc("https://pre.example.org/s.js", rtype=None) | {"response": {"status": 200, "content": {"mimeType": "application/javascript"}}},
                doc("https://www.example.com/b", pageref="p1", rtype=None),
            ],
        }
    }
    c = cap("n:c1", Artefact(id="n:har", type=ArtefactType.har, path=write(tmp_path, "n/x.har", json.dumps(har))))
    k = by_kind(run(Incident(id="n", source="synthetic", captures=[c]), tmp_path))
    assert k["landing_domain"] == {"example.com"}
    assert k["chain_domain"] == {"example.org"}
    assert k["redirect_chain_shape"] == {"start:hop>nav:landing"}


def test_local_file_har_gives_no_host(tmp_path):
    har = {"log": {"pages": [{"id": "p0", "title": "file:///C:/Users/x/page.html"}],
                   "entries": [doc("file:///C:/Users/x/page.html"), doc("https://cdn.example.org/a.js", rtype="script")]}}
    c = cap("f:c1", Artefact(id="f:har", type=ArtefactType.har, path=write(tmp_path, "f/x.har", json.dumps(har))))
    assert run(Incident(id="f", source="synthetic", captures=[c]), tmp_path) == []


# --------------------------------------------------------------------------- saved files


def test_saved_html(tmp_path):
    html = (
        f"<!-- saved from url=(0080){LANDING_URL} -->\n<html><head>"
        '<link rel="canonical" href="https://news.example.org/copied-article">'
        '<meta property="og:url" content="https://www.example.com/og"></head><body>x</body></html>'
    )
    c = cap("s:c1", Artefact(id="s:a0", type=ArtefactType.html, path=write(tmp_path, "s/page.html", html)))
    inds = run(Incident(id="s", source="synthetic", captures=[c]), tmp_path)
    k = by_kind(inds)
    assert k["landing_host"] == {"www.example.com"}  # the copied site's canonical is not the landing
    assert k["landing_domain"] == {"example.com"}
    assert all(i.observed is Observed.static for i in inds)
    assert {i.provenance.locator for i in inds} >= {"comment:saved-from", "meta[og:url]"}
    no_secrets(inds)


def test_canonical_counts_without_a_strong_source(tmp_path):
    html = '<html><head><link rel="canonical" href="https://shop.example.org/x"></head><body>x</body></html>'
    c = cap("w:c1", Artefact(id="w:a0", type=ArtefactType.html, path=write(tmp_path, "w/page.html", html)))
    assert by_kind(run(Incident(id="w", source="synthetic", captures=[c]), tmp_path))["landing_domain"] == {"example.org"}


def test_mhtml_snapshot_location(tmp_path):
    mhtml = (
        "From: <Saved by Blink>\r\nSnapshot-Content-Location: https://www.example.com/m?fbclid=1\r\n"
        'MIME-Version: 1.0\r\nContent-Type: multipart/related; type="text/html"; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html\r\nContent-Location: https://www.example.com/m?fbclid=1\r\n\r\n"
        "<html><body>x</body></html>\r\n--B--\r\n"
    )
    c = cap("m:c1", Artefact(id="m:a0", type=ArtefactType.mhtml, path=write(tmp_path, "m/page.mhtml", mhtml)))
    k = by_kind(run(Incident(id="m", source="synthetic", captures=[c]), tmp_path))
    assert k["landing_host"] == {"www.example.com"}


def test_fetcher_url_txt(tmp_path):
    text = f"https://trk.example.net/click?clickid=c0ffee\nhttps://cloak.example.org/p\n{LANDING_URL}\n"
    c = cap("fetch:x:c1", Artefact(id="fetch:x:url", type=ArtefactType.url, path=write(tmp_path, "x/url.txt", text)))
    inds = run(Incident(id="fetch:x", source="live_fetch", captures=[c]), tmp_path)
    k = by_kind(inds)
    assert k["landing_domain"] == {"example.com"}
    assert k["chain_domain"] == {"example.net", "example.org"}
    assert all(i.observed is Observed.live for i in inds)
    no_secrets(inds)


def test_organiser_url_file_is_static_landing(tmp_path):
    c = cap("o:c1", Artefact(id="o:url", type=ArtefactType.url, path=write(tmp_path, "o/link.txt", "see https://www.example.com/x\n")))
    inds = run(Incident(id="o", source="synthetic", captures=[c]), tmp_path)
    assert by_kind(inds)["landing_domain"] == {"example.com"}
    assert all(i.observed is Observed.static for i in inds)


# --------------------------------------------------------------------------- ad library and CERT


def test_adlib_capture(tmp_path):
    rec = {"landing_url": f"https://www.example.com/l?fbclid=1&access_token={TOKEN}", "display_domain": "EXAMPLE.ORG", "landing_domain": "example.net"}
    c = Capture(id="adlib:42:ad", stage=Stage.ad,
                artefacts=[Artefact(id="adlib:42:ad:library", type=ArtefactType.ad_library, path=write(tmp_path, "adlib-42/ad.json", json.dumps(rec)))])
    inds = run(Incident(id="adlib:42", source="adlib", captures=[c]), tmp_path)
    k = by_kind(inds)
    assert k["ad_landing_domain"] == {"example.com"}
    assert k["ad_display_domain"] == {"example.org"}
    assert {i.provenance.locator for i in inds} == {"$.landing_url", "$.display_domain"}
    roles_ds = Dataset(name="s", incidents=[Incident(id="adlib:42", source="adlib", captures=[c], indicators=[i.model_copy(update={"id": f"adlib:42:i{n}"}) for n, i in enumerate(inds)])])
    assert domains.domain_roles(roles_ds) == {"example.com": {"ad_landing"}, "example.org": {"ad_display"}}
    assert domains.registered_domains(roles_ds) == ["example.com"]


def test_certpl_listing(tmp_path):
    c = Capture(id="certpl:example.org:listing", stage=Stage.unknown,
                artefacts=[Artefact(id="certpl:example.org:listing:library", type=ArtefactType.url)])
    inc = Incident(id="certpl:example.org", source="certpl", source_ref="example.org", captures=[c])
    inds = run(inc, tmp_path)
    assert [(i.kind, i.value, i.provenance.artefact_id) for i in inds] == [("landing_domain", "example.org", "certpl:example.org:listing:library")]
    ds = extract_all(Dataset(name="s", incidents=[inc]), tmp_path)
    assert domains.domain_roles(ds) == {"example.org": {"certpl"}}
    assert domains.registered_domains(ds) == ["example.org"]


# --------------------------------------------------------------------------- kit agrees


def test_kit_own_domains_equal_landing_and_chain(tmp_path):
    html = f"<!-- saved from url=(0080){LANDING_URL} -->\n<html><body><img src='https://collect.example.edu/p.gif'></body></html>"
    c = cap(
        "k:c1",
        Artefact(id="k:har", type=ArtefactType.har, path=write(tmp_path, "k/page.har", json.dumps(chain_har()))),
        Artefact(id="k:a0", type=ArtefactType.html, path=write(tmp_path, "k/page.html", html)),
    )
    inc = Incident(id="k", source="synthetic", captures=[c])
    k = by_kind(run(inc, tmp_path))
    own = _pages.own_domains(_pages.page_chain(c, tmp_path))
    assert own == k["landing_domain"] | k["chain_domain"]
    assert {registrable_domain(h) for h in k["landing_host"] | k["chain_host"]} == own
    third = by_kind(kit.extract(inc, c, tmp_path))["third_party_domain"]
    assert not third & own
    assert "example.edu" in third  # the pixel and the iframe are third parties


def test_missing_files_are_skipped(tmp_path):
    c = cap("x:c1", Artefact(id="x:a0", type=ArtefactType.har, path="nope.har"), Artefact(id="x:a1", type=ArtefactType.html))
    assert run(Incident(id="x", source="s", captures=[c]), tmp_path) == []


def test_malformed_location_and_url_do_not_crash(tmp_path):
    har = {"log": {"entries": [doc("https://a.example.net/x", 302, "http://["), doc("https://[x/")]}}
    c = cap("b:c1", Artefact(id="b:har", type=ArtefactType.har, path=write(tmp_path, "b/x.har", json.dumps(har))))
    inc = Incident(id="b", source="synthetic", captures=[c])
    assert by_kind(run(inc, tmp_path))["landing_domain"] == {"example.net"}
    list(kit.extract(inc, c, tmp_path))


def test_same_domain_hop_is_not_a_chain_value(tmp_path):
    har = {"log": {"entries": [doc("http://example.com/", 301, "https://www.example.com/"), doc("https://www.example.com/")]}}
    c = cap("d:c1", Artefact(id="d:har", type=ArtefactType.har, path=write(tmp_path, "d/x.har", json.dumps(har))))
    k = by_kind(run(Incident(id="d", source="synthetic", captures=[c]), tmp_path))
    assert "chain_host" not in k and "chain_domain" not in k
    assert k["redirect_chain_shape"] == {"start:self>301:landing"}


def test_generic_redirect_is_not_a_tracker():
    assert not domains.looks_like_tracker("https://a.example.net/go?cid=42")
    assert domains.looks_like_tracker("https://a.example.net/click?x=1")


def test_mhtml_canonical_is_weak_and_protocol_relative_url_file(tmp_path):
    mhtml = (
        'MIME-Version: 1.0\r\nContent-Type: multipart/related; type="text/html"; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html\r\n\r\n"
        '<html><head><link rel="canonical" href="https://www.example.org/c"></head><body>x</body></html>\r\n--B--\r\n'
    )
    c = cap("mc:c1", Artefact(id="mc:a0", type=ArtefactType.mhtml, path=write(tmp_path, "mc/p.mhtml", mhtml)))
    assert by_kind(run(Incident(id="mc", source="synthetic", captures=[c]), tmp_path))["landing_domain"] == {"example.org"}
    c2 = cap("pr:c1", Artefact(id="pr:url", type=ArtefactType.url, path=write(tmp_path, "pr/u.txt", "//www.example.net/p\n")))
    assert by_kind(run(Incident(id="pr", source="synthetic", captures=[c2]), tmp_path))["landing_domain"] == {"example.net"}
