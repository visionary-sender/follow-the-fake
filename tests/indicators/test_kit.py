"""Kit fingerprints over synthetic pages (no organiser data; reserved example domains only)."""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from pathlib import Path

import pytest

from ftf.contract import Artefact, ArtefactType, Capture, Dataset, Incident, Layer, Observed, Stage
from ftf.indicators import _kitnorm as kn
from ftf.indicators import extract_all, kit

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2  # same bytes in both kits
KIT_JS = """/* kit bundle v1 */
function showComments(list) {
    // render the fake comments
    for (var i = 0; i < list.length; i++) {
        document.getElementById('c' + i).style.display = 'block';
    }
}
"""
KIT_JS_REMINIFIED = "function showComments(list){for(var i=0;i<list.length;i++){document.getElementById('c'+i).style.display='block';}}"
KIT_CSS = ".a{color:red}\n.b { margin: 0 auto; }\n/* footer */\n.c{padding:4px}\n" * 4
TOKEN = "EAABsbCS1iHgBAKZBZCxyz0123456789"


def kit_page(domain: str, cls: str, pixel: str, *, extra: int = 0, assets: str = "files") -> str:
    """A 'same kit' landing: same structure and script logic, randomised names and ids."""
    comments = "".join(
        f'<div class="{cls}-c{n}"><img src="{assets}/av{n}{cls}.png"><p class="{cls}-t">Comment {n}</p><span>1h</span></div>'
        for n in range(8 + extra)
    )
    return f"""<!DOCTYPE html>
<!-- saved from url=(0060)https://{domain}/land/?utm_id=k1b2c3d4ez&utm_visit=0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b&access_token={TOKEN} -->
<html lang="pl" style="filter: hue-rotate(4deg)"><head><meta charset="utf-8"><title>Breaking {cls}</title>
<link rel="stylesheet" href="{assets}/s{cls}.css">
<script src="{assets}/m{cls}.js.pobrane"></script>
<script>
  // per-deployment config
  var cfg_{cls}9z8 = {{ pixel: "{pixel}", host: "https://{domain}/api", visit: "{cls}AbC123xyz789" }};
  fbq('init', cfg_{cls}9z8.pixel); fbq('track', 'PageView', {{ ts: {int(pixel) % 100000} }});
</script>
<script src="https://collect.example.edu/t.js?k={pixel}"></script>
</head><body class="{cls}-body"><header class="{cls}-h"><nav><ul><li><a href="#">A</a></li><li><a href="#">B</a></li></ul></nav></header>
<main id="{cls}-main"><article><h1 class="{cls}-x">Headline</h1><p>Intro</p><p>Body</p><figure><img src="{assets}/hero{cls}.png"><figcaption>x</figcaption></figure>
<form action="/order.php?sub1={cls}&pid={pixel}" method="post"><input name="name"><input name="phone"><button>Go</button></form>
<section class="{cls}-comments">{comments}</section></article></main><footer><p>(c)</p></footer></body></html>"""


def unrelated_page() -> str:
    rows = "".join(f"<tr><td>{n}</td><td><b>Item</b></td><td><i>{n * 3}</i></td></tr>" for n in range(12))
    return f"""<!DOCTYPE html>
<!-- saved from url=(0040)https://shop.example.org/catalog?category=shoes -->
<html><head><title>Shop</title><script>
document.addEventListener('DOMContentLoaded', function () {{ window.cart = []; alert('welcome'); }});
</script><script src="https://stats.example.org/s.js"></script></head>
<body><table>{rows}</table><form action="/search?q=x"><input name="q"></form></body></html>"""


def har(domain: str, js_body: str, extra_host: str, query: str) -> dict:
    return {
        "log": {
            "version": "1.2",
            "pages": [{"id": "p", "title": f"https://{domain}/land/?{query}"}],
            "entries": [
                {
                    "_resourceType": "document",
                    "request": {"method": "GET", "url": f"https://{domain}/land/?{query}"},
                    "response": {"status": 200, "content": {"mimeType": "text/html", "text": "<html><body><p>x</p></body></html>"}},
                },
                {
                    "request": {"method": "GET", "url": f"https://{domain}/_cdn/zz{len(domain)}.js"},
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/javascript", "encoding": "base64", "text": base64.b64encode(js_body.encode()).decode()},
                    },
                },
                {
                    "request": {"method": "GET", "url": f"https://{extra_host}/pixel?id=1"},
                    "response": {"status": 200, "content": {"mimeType": "image/gif", "text": ""}},
                },
            ],
        }
    }


def write_capture(root: Path, name: str, html: str, files: dict[str, bytes], har_doc: dict | None) -> Capture:
    folder = root / name
    (folder / "page_files").mkdir(parents=True)
    (folder / "page.html").write_text(html, encoding="utf-8")
    arts = [Artefact(id=f"{name}:a0", type=ArtefactType.html, path=f"{name}/page.html")]
    for n, (fname, data) in enumerate(files.items(), start=1):
        (folder / "page_files" / fname).write_bytes(data)
        arts.append(Artefact(id=f"{name}:a{n}", type=ArtefactType.asset, path=f"{name}/page_files/{fname}"))
    if har_doc is not None:
        (folder / "site.har").write_text(json.dumps(har_doc), encoding="utf-8")
        arts.append(Artefact(id=f"{name}:har", type=ArtefactType.har, path=f"{name}/site.har"))
    return Capture(id=f"{name}:c1", stage=Stage.lander, artefacts=arts)


QUERY_A = f"utm_id=k1b2c3d4ez&utm_visit=0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b&access_token={TOKEN}"
QUERY_B = "utm_id=z9y8x7w6v5&utm_visit=0190ffff-0000-7e5f-8a9b-0c1d2e3f4a5b&access_token=EAAother0123456789abcdef"


@pytest.fixture
def captures(tmp_path: Path) -> dict[str, Capture]:
    a = write_capture(
        tmp_path,
        "a",
        kit_page("news.example.com", "qx7", "1234567890123456"),
        {"hero_qx7.png": PNG, "mqx7.js.pobrane": KIT_JS.encode(), "sqx7.css": KIT_CSS.encode()},
        har("news.example.com", KIT_JS, "collect.example.edu", QUERY_A),
    )
    b = write_capture(
        tmp_path,
        "b",
        kit_page("daily.example.net", "Lm2", "9876543210987654"),
        {"photo-q7x-101.png": PNG, "bundle_Lm2.js": KIT_JS_REMINIFIED.encode(), "x.css": KIT_CSS.encode()},
        har("daily.example.net", KIT_JS_REMINIFIED, "collect.example.edu", QUERY_B),
    )
    near = write_capture(
        tmp_path,
        "near",
        kit_page("third.example.net", "Rr5", "5555555555555555", extra=1),
        {},
        None,
    )
    other = write_capture(
        tmp_path,
        "other",
        unrelated_page(),
        {"logo.png": b"\x89PNG\r\n\x1a\n" + b"\x01\x02" * 300, "app.js": b"console.log('shop ready'); var cart = [];" * 5},
        har("shop.example.org", "console.log('other');" * 10, "stats.example.org", "category=shoes"),
    )
    return {"a": a, "b": b, "near": near, "other": other, "root": tmp_path}  # type: ignore[dict-item]


def run(captures, name):
    cap = captures[name]
    inc = Incident(id=name, source="synthetic", captures=[cap])
    return list(kit.extract(inc, cap, captures["root"]))


def by_kind(indicators) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for ind in indicators:
        out[ind.kind].add(ind.value)
    return out


def shared(x, y) -> dict[str, int]:
    kx, ky = by_kind(x), by_kind(y)
    return {k: len(kx[k] & ky[k]) for k in kx if kx[k] & ky.get(k, set())}


def test_same_kit_pages_share_every_fingerprint_family(captures):
    common = shared(run(captures, "a"), run(captures, "b"))
    for kind in (
        "asset_sha256",
        "asset_norm_sha256",
        "inline_script_sha256",
        "dom_skeleton_sha256",
        "dom_skeleton_lsh",
        "page_url_shape",
        "form_action_shape",
        "third_party_host",
        "third_party_domain",
    ):
        assert common.get(kind), f"{kind} not shared: {common}"


def test_reminified_asset_matches_only_after_normalisation(captures):
    a, b = by_kind(run(captures, "a")), by_kind(run(captures, "b"))
    js_a, js_b = kn.sha256(KIT_JS.encode()), kn.sha256(KIT_JS_REMINIFIED.encode())
    assert js_a in a["asset_sha256"] and js_b in b["asset_sha256"] and js_a not in b["asset_sha256"]
    assert kn.sha256(kn.normalise_text(KIT_JS).encode()) in a["asset_norm_sha256"] & b["asset_norm_sha256"]


def test_near_identical_structure_shares_lsh_band_not_exact_hash(captures):
    a, near = by_kind(run(captures, "a")), by_kind(run(captures, "near"))
    assert not a["dom_skeleton_sha256"] & near["dom_skeleton_sha256"]
    assert a["dom_skeleton_lsh"] & near["dom_skeleton_lsh"]


def test_unrelated_page_shares_nothing(captures):
    other = run(captures, "other")
    for name in ("a", "b", "near"):
        assert shared(run(captures, name), other) == {}


def test_own_domain_is_not_third_party(captures):
    hosts = by_kind(run(captures, "a"))["third_party_host"]
    assert "collect.example.edu" in hosts
    assert not any(h.endswith("example.com") for h in hosts)


def test_no_parameter_values_or_tokens_in_output(captures):
    for name in ("a", "b"):
        for ind in run(captures, name):
            assert "EAA" not in ind.value and "k1b2c3d4ez" not in ind.value and "1234567890123456" not in ind.value
    shapes = by_kind(run(captures, "a"))["page_url_shape"]
    assert "access_token={alnum32}&utm_id={alnum10}&utm_visit={uuid}" in shapes


def test_observed_and_provenance(captures):
    cap = captures["a"]
    ids = {art.id for art in cap.artefacts}
    inds = run(captures, "a")
    assert all(i.provenance.artefact_id in ids for i in inds)
    live = [i for i in inds if i.observed is Observed.live]
    assert live and all(i.provenance.artefact_id == "a:har" for i in live)
    assert all(i.provenance.artefact_id != "a:har" for i in inds if i.observed is Observed.static)
    assert all(i.extractor.startswith("kit@") for i in inds)
    for i in inds:
        assert (i.layer, i.level) == kit.kinds()[i.kind]
    assert {i.layer for i in inds} <= {Layer.kit, Layer.landing, Layer.infrastructure}


def test_extract_all_validates(captures):
    incs = [Incident(id=n, source="synthetic", captures=[captures[n]]) for n in ("a", "b", "other")]
    ds = extract_all(Dataset(name="synthetic", incidents=incs), captures["root"])
    assert any(ind.extractor.startswith("kit@") for ind in ds.indicators())


def test_missing_and_malformed_files_are_skipped(tmp_path):
    (tmp_path / "bad.har").write_text("{not json")
    (tmp_path / "empty.html").write_text("")
    cap = Capture(
        id="x:c1",
        stage=Stage.lander,
        artefacts=[
            Artefact(id="x:a0", type=ArtefactType.har, path="bad.har"),
            Artefact(id="x:a1", type=ArtefactType.html, path="empty.html"),
            Artefact(id="x:a2", type=ArtefactType.asset, path="does-not-exist.png"),
            Artefact(id="x:a3", type=ArtefactType.asset),
        ],
    )
    assert list(kit.extract(Incident(id="x", source="s", captures=[cap]), cap, tmp_path)) == []


def test_mhtml_parts(tmp_path):
    html = kit_page("news.example.com", "qx7", "1234567890123456")
    mhtml = (
        "From: <Saved by Blink>\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; type="text/html"; boundary="BOUND"\r\n\r\n'
        "--BOUND\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: base64\r\n"
        "Content-Location: https://news.example.com/land/?a=1\r\n\r\n"
        + base64.encodebytes(html.encode()).decode()
        + "\r\n--BOUND\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
        "Content-Location: https://img.example.edu/x.png\r\n\r\n"
        + base64.encodebytes(PNG).decode()
        + "\r\n--BOUND--\r\n"
    )
    (tmp_path / "page.mhtml").write_text(mhtml)
    cap = Capture(id="m:c1", stage=Stage.lander, artefacts=[Artefact(id="m:a0", type=ArtefactType.mhtml, path="page.mhtml")])
    found = by_kind(kit.extract(Incident(id="m", source="s", captures=[cap]), cap, tmp_path))
    assert kn.sha256(PNG) in found["asset_sha256"]
    assert found["dom_skeleton_sha256"] and found["inline_script_sha256"]
    assert "img.example.edu" in found["third_party_host"]
    assert "a={num1}" in found["page_url_shape"]


# ------------------------------------------------------------------ normalisers


def test_normalise_script_masks_deployment_values():
    a = 'var _0x1a2b = "https://alpha.example.com/x"; fbq("init", "1234567890123456"); var t = 1700000000;'
    b = 'var _0x9f8e = "https://beta.example.net/y"; fbq("init", "6543210987654321"); var t = 1800000000;'
    assert kn.normalise_script(a) == kn.normalise_script(b)
    assert kn.normalise_script('show("hello")') != kn.normalise_script('show("bye")')


def test_param_shape_types_and_tokens():
    assert kn.param_shape("b=12&a=&c=deadbeef&d=0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b") == "a={empty}&b={num2}&c={hex8}&d={uuid}"
    assert kn.param_shape("") is None
    assert kn.param_shape("EAABsbCS1iHgBAKZBZCxyz=1") == "{token}={num1}"


def test_sniff():
    assert kn.sniff(PNG) == "image"
    assert kn.sniff(b"wOF2\x00\x01") == "font"
    assert kn.sniff(b"<!DOCTYPE html><html></html>") == "html"
    assert kn.sniff(b"body{color:red}") == "text"
    assert kn.sniff(b"\x00\x01\x02binary") == "binary"


# ------------------------------------------------------------------ review regressions


def _one(tmp_path: Path, art_type: ArtefactType, name: str, content: str):
    (tmp_path / name).write_text(content)
    cap = Capture(id="r:c1", stage=Stage.lander, artefacts=[Artefact(id="r:a0", type=art_type, path=name)])
    return by_kind(kit.extract(Incident(id="r", source="s", captures=[cap]), cap, tmp_path))


def test_protocol_relative_and_canonical_own_domain(tmp_path):
    html = (
        '<html><head><link rel="canonical alternate" href="https://own.example.com/p?x=1">'
        '<script src="//kit-cdn.example.edu/app.js"></script></head>'
        '<body><img src="https://own.example.com/img/a.png"></body></html>'
    )
    found = _one(tmp_path, ArtefactType.html, "p.html", html)
    assert "kit-cdn.example.edu" in found["third_party_host"]
    assert not any(h.endswith("own.example.com") for h in found["third_party_host"])


def test_har_redirect_chain_is_own(tmp_path):
    doc = {
        "log": {
            "entries": [
                {
                    "_resourceType": "document",
                    "request": {"url": "https://short.example.net/abc"},
                    "response": {"status": 302, "redirectURL": "https://landing.example.com/", "content": {}},
                },
                {
                    "_resourceType": "document",
                    "request": {"url": "https://landing.example.com/"},
                    "response": {"status": 200, "content": {"mimeType": "text/html", "text": "<html><body>x</body></html>"}},
                },
                {"request": {"url": "https://landing.example.com/a.js"}, "response": {"content": "not a dict"}},
                {"request": "GET broken", "response": []},
            ]
        }
    }
    found = _one(tmp_path, ArtefactType.har, "s.har", json.dumps(doc))
    assert not any("landing.example.com" in h for h in found["third_party_host"])


def test_unparseable_html_body_is_skipped(tmp_path):
    _one(tmp_path, ArtefactType.html, "c.html", "<!-- only a comment -->\x01\x02")  # must not raise


def test_short_af_words_are_not_hex():
    assert kn.param_shape("utm_source=fb") == kn.param_shape("utm_source=ig")
    assert kn.value_type("fb") == "{alnum2}" and kn.value_type("deadbeef") == "{hex8}"
