"""platform extractor on small synthetic captures (fake ids, example.test domains)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Capture, Dataset, Incident, Layer, Level, Observed, Stage
from ftf.indicators import extract_all
from ftf.indicators import platform
from ftf.indicators._ids import decode_xid, norm_phone, scan, strip_tokens

PIXEL = "1234567890123456"
PIXEL2 = "6543210987654321"
TENANT = "0a1b2c3d-0000-4000-8000-00000000000a"
LANDING = "0a1b2c3d-0000-4000-8000-00000000000b"
OWNER = "0a1b2c3d-0000-4000-8000-00000000000c"
CAMPAIGN = "0a1b2c3d-0000-4000-8000-00000000000d"
TEMPLATE = "0a1b2c3d-0000-4000-8000-00000000000e"
GATEWAY = "xd-0123456789abcdef0123456789abcdef.ecs.us-east-1.on.aws"
TOKEN = "EAAB" + "x7Qz" * 12
# xid: time 2026-10-08 (0x6a...), machine abcdef, pid 50061, counter 1.
XID_BYTES = (1791460000).to_bytes(4, "big") + bytes.fromhex("abcdef") + (50061).to_bytes(2, "big") + (1).to_bytes(3, "big")
XID = base64.b32hexencode(XID_BYTES).decode().lower().rstrip("=")


def _aio_page() -> str:
    cfg = {
        "campaign": {"uuid": CAMPAIGN, "owner_uuid": OWNER.upper(), "name": "Team / XX / Camp9"},
        "landing": {"uuid": LANDING, "name": "Test landing 'quoted'", "lander_template_uuid": TEMPLATE},
        "source": {"uuid": "0a1b2c3d-0000-4000-8000-00000000000f", "name": "FB"},
        "handle": {"key": "00112233445566778899aabbccddeeff", "value": "x"},
        "visit": {
            "ip_address": "198.51.100.7",
            "fingerprint": "ffffffffffffffffffffffffffffffff",
            "flow_uuid": "0a1b2c3d-0000-4000-8000-000000000010",
            "query": {"ad_id": "120200000000000001", "ad_name": "BUYER/XX/Dr Test/abc", "access_token": TOKEN},
        },
    }
    js = json.dumps(cfg).replace("'", "\\'").replace("/", "\\/")
    return f"""<!doctype html><html style="filter: hue-rotate(4deg)"><head>
<script>window.aio = JSON.parse('{js}');</script>
<script src="https://cdn.example.test/aio-static/sdk/main.js?v=9.9.9"></script>
<link href="/_cdn/production/landing-cdn/{TENANT}/{LANDING}/style.css">
<script>fbq('init', '{PIXEL}');</script>
<script async src="https://www.googletagmanager.com/gtag/js?id=AW-123456789"></script>
</head><body>
<a href="https://land.example.test/?ad_id=120200000000000001&amp;adset_name=Adset%20One&amp;access_token={TOKEN}">x</a>
<form action="/lead-submit.php?x=1" method="post">
  <input name="Name"><input name="phone" placeholder="+48 123 456 789"><input type="hidden" name="fbpx" value="{PIXEL}">
  <input type="submit" name="go">
</form>
<a href="tel:+48 601 234 567">call</a> <a href="mailto:Sales@Operator.example">m</a> <a href="https://t.me/share/url">s</a>
<a href="https://t.me/Operator_Desk">tg</a>
</body></html>"""


def _har_doc() -> dict:
    def entry(url, method="GET", post=None, mime="", body=None, body_mime="text/html", location=""):
        e = {
            "request": {"method": method, "url": url, "headers": []},
            "response": {"status": 302 if location else 200, "headers": [{"name": "Location", "value": location}] if location else [],
                         "content": {"mimeType": body_mime}},
        }
        if post is not None:
            e["request"]["postData"] = {"mimeType": mime, "text": post}
        if body is not None:
            e["response"]["content"]["text"] = base64.b64encode(body.encode()).decode()
            e["response"]["content"]["encoding"] = "base64"
        return e

    return {"log": {"entries": [
        entry(f"https://www.facebook.com/tr/?id={PIXEL}&ev=PageView"),
        entry(f"https://{GATEWAY}/events", "POST", json.dumps({"fb.pixel_id": PIXEL2}), "application/json"),
        entry(f"https://post.example.test/click?upd_clickid={XID}&event1=1"),
        entry(f"https://land.example.test/p?access_token={TOKEN}&campaign_id=120200000000000002"),
        entry("https://land.example.test/checkout.php", "POST", "name=a&phone=b&offer_id=77001", "application/x-www-form-urlencoded"),
        entry("https://land.example.test/", body=f"<script>fbq('init','{PIXEL2}')</script>"),
        entry("https://land.example.test/r", location="https://next.example.test/?ad_name=Moved%20Ad"),
        entry("data:image/png;base64,AAAA"),
    ]}}


def _capture(tmp_path: Path) -> tuple[Incident, Capture]:
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "page.html").write_text(_aio_page(), encoding="utf-8")
    (tmp_path / "case" / "net.har").write_text(json.dumps(_har_doc()), encoding="utf-8")
    (tmp_path / "case" / "page_files").mkdir()
    (tmp_path / "case" / "page_files" / f"{PIXEL}.pobrane").write_text("fbq.registerPlugin(1)", encoding="utf-8")
    (tmp_path / "case" / "page_files" / "jquery.min.js").write_text(f"fbq('init','{PIXEL2}9')", encoding="utf-8")
    cap = Capture(
        id="t-1/lander",
        stage=Stage.lander,
        artefacts=[
            Artefact(id="t-1/lander/a0", type=ArtefactType.html, path="case/page.html"),
            Artefact(id="t-1/lander/a1", type=ArtefactType.har, path="case/net.har"),
            Artefact(id="t-1/lander/a2", type=ArtefactType.asset, path=f"case/page_files/{PIXEL}.pobrane"),
            Artefact(id="t-1/lander/a3", type=ArtefactType.asset, path="case/page_files/jquery.min.js"),
            Artefact(id="t-1/lander/a4", type=ArtefactType.html, path="case/missing.html"),
        ],
    )
    return Incident(id="t-1", source="synthetic", captures=[cap]), cap


def _index(inds):
    out: dict[tuple[str, str], set[Observed]] = {}
    for i in inds:
        out.setdefault((i.kind, i.value), set()).add(i.observed)
    return out


def test_identifier_families_and_observed(tmp_path):
    inc, cap = _capture(tmp_path)
    got = _index(platform.extract(inc, cap, tmp_path))
    live, static = {Observed.live}, {Observed.static}
    # platform / tenant / owner / landing (aio)
    assert got[("platform_name", "aio")] == static
    assert got[("aio_sdk_asset", "main.js?v=9.9.9")] == static
    assert got[("aio_cdn_tenant", TENANT)] == static
    assert got[("aio_landing", LANDING)] == static
    assert got[("aio_owner", OWNER)] == static  # upper-case input normalised
    assert got[("aio_campaign", CAMPAIGN)] == static
    assert got[("aio_campaign_name", "Team / XX / Camp9")] == static
    assert got[("aio_landing_name", "Test landing 'quoted'")] == static
    assert got[("aio_lander_template", TEMPLATE)] == static
    assert got[("aio_handle_key", "00112233445566778899aabbccddeeff")] == static
    assert ("aio_flow", "0a1b2c3d-0000-4000-8000-000000000010") in got
    # tracking accounts
    assert got[("meta_pixel", PIXEL)] == live | static
    assert got[("meta_pixel", PIXEL2)] == live | static  # gateway body live, HAR response body static
    assert got[("capi_gateway", GATEWAY)] == live
    assert got[("google_ads_id", "AW-123456789")] == static
    # tracker instance
    assert got[("xid_tracker_machine", "abcdef")] == live
    assert got[("tracker_postback", "post.example.test/click")] == live
    # distribution / creative
    assert got[("meta_ad_id", "120200000000000001")] == static
    assert got[("meta_ad_name", "BUYER/XX/Dr Test/abc")] == static
    assert got[("meta_adset_name", "Adset One")] == static
    assert got[("meta_campaign_id", "120200000000000002")] == live
    assert got[("meta_ad_name", "Moved Ad")] == live
    # offer: forms and contacts
    assert got[("form_endpoint", "/lead-submit.php")] == static
    assert got[("form_field_set", "fbpx,name,phone")] == static
    assert got[("form_endpoint", "/checkout.php")] == live
    assert got[("form_field_set", "name,offer_id,phone")] == live
    assert got[("offer_id", "77001")] == live
    assert got[("contact_phone", "+48601234567")] == static
    assert got[("contact_email", "sales@operator.example")] == static
    assert got[("contact_telegram", "operator_desk")] == static
    # negatives: placeholder phone, generic t.me share, library contents, visitor fields
    kinds_values = set(got)
    assert ("contact_phone", "+48123456789") not in kinds_values
    assert ("contact_telegram", "share") not in kinds_values
    assert not any(v == PIXEL2 + "9" for _, v in kinds_values)
    assert not any("198.51.100.7" in v or "ffffffff" in v for _, v in kinds_values)


def test_layers_levels_provenance(tmp_path):
    inc, cap = _capture(tmp_path)
    inds = list(platform.extract(inc, cap, tmp_path))
    art_ids = {a.id for a in cap.artefacts}
    for i in inds:
        assert i.extractor.startswith("platform@")
        assert i.provenance.artefact_id in art_ids
        assert i.provenance.locator
        assert i.layer is not Layer.collector
    by = {(i.kind, i.value): i for i in inds}
    assert by[("aio_cdn_tenant", TENANT)].layer is Layer.tenant
    assert by[("aio_owner", OWNER)].level is Level.operator
    assert by[("capi_gateway", GATEWAY)].layer is Layer.tracking_account
    assert by[("capi_gateway", GATEWAY)].provenance.locator == "har:entries[1].request.url"
    assert by[("xid_tracker_machine", "abcdef")].layer is Layer.tracker_instance
    assert by[("meta_ad_id", "120200000000000001")].layer is Layer.creative
    assert by[("form_field_set", "fbpx,name,phone")].layer is Layer.offer


def test_access_tokens_never_leak(tmp_path, capsys):
    inc, cap = _capture(tmp_path)
    ds = extract_all(Dataset(name="t", incidents=[inc]), tmp_path)
    dumped = ds.to_json()
    assert "EAAB" not in dumped and "x7Qz" not in dumped
    for i in platform.extract(inc, cap, tmp_path):
        assert "EAA" not in i.model_dump_json()
        assert "token" not in (i.value or "").lower()
    out = capsys.readouterr()
    assert "x7Qz" not in out.out + out.err


def test_strip_tokens():
    s = f"https://a.example.test/?x=1&access_token={TOKEN}&fb_token=abc&y=2 bare {TOKEN} end"
    r = strip_tokens(s)
    assert "x7Qz" not in r and "abc" not in r
    assert "x=1" in r and "y=2" in r
    assert not any(h.value.startswith("EAA") for h in scan(s))


def test_xid_decode():
    x = decode_xid(XID)
    assert x is not None and x.machine == "abcdef" and x.pid == 50061 and x.counter == 1
    assert x.time.year == 2026
    assert decode_xid("zzzzzzzzzzzzzzzzzzzz") is None  # not base32hex
    assert decode_xid("00000000000000000000") is None  # implausible time


def test_phone_normalisation():
    assert norm_phone("+48 601-234-567") == "+48601234567"
    assert norm_phone("601 234 567") == "+48601234567"
    assert norm_phone("0048601234567") == "+48601234567"
    assert norm_phone("+420 777 123 456") == "+420777123456"
    assert norm_phone("123 456 789") is None
    assert norm_phone("+48 500 000 000") is None
    assert norm_phone("12345") is None


def test_equal_identifiers_compare_equal():
    a = {(h.kind, h.value) for h in scan(f"fbq('init', '{PIXEL}'); <a href='tel:601234567'>")}
    b = {(h.kind, h.value) for h in scan(f"https://www.facebook.com/tr?ev=Lead&id={PIXEL} +48 601 234 567")}
    assert ("meta_pixel", PIXEL) in a & b
    assert ("contact_phone", "+48601234567") in a & b


def test_mhtml_and_dead_inputs(tmp_path):
    body = f"<html><script>fbq('init','{PIXEL}')</script><form action='https://x.example.test/api/lead.php'><input name='a'></form></html>"
    mht = (
        "MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"B\"\r\n\r\n--B\r\n"
        "Content-Type: text/html; charset=bogus-charset\r\nContent-Transfer-Encoding: quoted-printable\r\n"
        "Content-Location: https://x.example.test/\r\n\r\n" + body.replace("=", "=3D") + "\r\n--B--\r\n"
    )
    (tmp_path / "p.mhtml").write_text(mht, encoding="utf-8")
    (tmp_path / "bad.har").write_text("{not json", encoding="utf-8")
    cap = Capture(id="m-1/lander", stage=Stage.lander, artefacts=[
        Artefact(id="m-1/lander/a0", type=ArtefactType.mhtml, path="p.mhtml"),
        Artefact(id="m-1/lander/a1", type=ArtefactType.har, path="bad.har"),
        Artefact(id="m-1/lander/a2", type=ArtefactType.screenshot, path="shot.png"),
    ])
    inc = Incident(id="m-1", source="synthetic", captures=[cap])
    got = _index(platform.extract(inc, cap, tmp_path))
    assert got[("meta_pixel", PIXEL)] == {Observed.static}
    assert got[("form_endpoint", "/api/lead.php")] == {Observed.static}


def test_endpoint_and_email_normalisation():
    from ftf.indicators._ids import norm_email, norm_path

    assert norm_path("https://x.example.test/lander/../Checkout.php?a=1#f") == "/checkout.php"
    assert norm_path("api//lead.php") == "/api/lead.php"
    assert norm_path(f"/api/v1/trigger/field/{LANDING}/") == "/api/v1/trigger/field/{uuid}/"
    assert norm_path("#") is None and norm_path("javascript:void(0)") is None
    assert norm_email("css-0a1b2c3d@mhtml.blink") is None
    assert norm_email("robert@broofa.com") is None
    assert norm_email("Desk@Operator.example.") == "desk@operator.example"


def test_flow_list_as_string_and_video_tenant():
    from ftf.indicators._html import aio_hits

    flows = "{0: 0a1b2c3d-0000-4000-8000-000000000011, 1: 0A1B2C3D-0000-4000-8000-000000000012}"
    got = {(h.kind, h.value) for h, _ in aio_hits({"visit": {"flow_uuids": flows}})}
    assert ("aio_flow", "0a1b2c3d-0000-4000-8000-000000000011") in got
    assert ("aio_flow", "0a1b2c3d-0000-4000-8000-000000000012") in got
    hits = {(h.kind, h.value) for h in scan("src=https://vz-0a1b2c3d-0ab.tv.video.example.test/x vz-0a1b2c3d-0abc")}
    assert hits == {("video_cdn_tenant", "vz-0a1b2c3d-0ab")}


def test_beacon_posts_are_not_forms(tmp_path):
    har = {"log": {"entries": [{
        "request": {"method": "POST", "url": "https://www.facebook.com/tr/",
                    "postData": {"mimeType": "application/x-www-form-urlencoded", "text": f"id={PIXEL}&ev=Lead"}},
        "response": {"status": 200, "headers": [], "content": {}},
    }]}}
    (tmp_path / "n.har").write_text(json.dumps(har), encoding="utf-8")
    cap = Capture(id="b-1/lander", stage=Stage.lander, artefacts=[Artefact(id="b-1/lander/a0", type=ArtefactType.har, path="n.har")])
    got = _index(platform.extract(Incident(id="b-1", source="synthetic", captures=[cap]), cap, tmp_path))
    assert not any(k in ("form_endpoint", "form_field_set") for k, _ in got)


def _har_capture(tmp_path: Path, entries: list, cid: str = "h-1") -> tuple[Incident, Capture]:
    (tmp_path / f"{cid}.har").write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    cap = Capture(id=f"{cid}/lander", stage=Stage.lander,
                  artefacts=[Artefact(id=f"{cid}/lander/a0", type=ArtefactType.har, path=f"{cid}.har")])
    return Incident(id=cid, source="synthetic", captures=[cap]), cap


def _resp(body: str, mime: str = "text/html") -> dict:
    return {"status": 200, "headers": [], "content": {"mimeType": mime, "text": body}}


def test_collector_form_input_is_not_an_operator_contact(tmp_path):
    entries = [
        {"request": {"method": "POST", "url": "https://land.example.test/api/lead",
                     "postData": {"mimeType": "application/json",
                                  "text": '{"email":"analyst@cert.example","phone":"+48 601 234 567"}'}},
         "response": _resp("<p>Thanks analyst@cert.example, we call +48 601 234 567</p>")},
        {"request": {"method": "GET", "url": "https://land.example.test/ok?email=other%40cert.example"},
         "response": _resp("<a href='tel:+48602345678'>desk</a>")},
    ]
    inc, cap = _har_capture(tmp_path, entries)
    got = _index(platform.extract(inc, cap, tmp_path))
    assert ("contact_email", "analyst@cert.example") not in got
    assert ("contact_phone", "+48601234567") not in got
    assert ("contact_email", "other@cert.example") not in got
    assert got[("contact_phone", "+48602345678")] == {Observed.static}


def test_malformed_har_entries_are_skipped(tmp_path):
    entries = [
        {"request": "GET /", "response": {}},
        {"request": {"method": "POST", "url": "https://land.example.test/x", "postData": "name=a"}, "response": "oops"},
        {"request": {"url": f"https://www.facebook.com/tr/?id={PIXEL}"}, "response": {"headers": "bad", "content": []}},
    ]
    inc, cap = _har_capture(tmp_path, entries)
    got = _index(platform.extract(inc, cap, tmp_path))
    assert got[("meta_pixel", PIXEL)] == {Observed.live}


def test_library_bodies_skipped_kit_scripts_read(tmp_path):
    entries = [
        {"request": {"method": "GET", "url": "https://cdn.example.test/js/jquery-3.7.1.min.js"},
         "response": _resp("/* author@jquery.example.org +48 602 345 678 */", "application/javascript")},
        {"request": {"method": "GET", "url": "https://land.example.test/js/slick-init.js?v=2"},
         "response": _resp(f"fbq('init', '{PIXEL2}')", "application/javascript")},
    ]
    inc, cap = _har_capture(tmp_path, entries)
    got = _index(platform.extract(inc, cap, tmp_path))
    assert ("contact_phone", "+48602345678") not in got
    assert got[("meta_pixel", PIXEL2)] == {Observed.static}


def test_pixel_filename_needs_signals_content(tmp_path):
    (tmp_path / "p_files").mkdir()
    (tmp_path / "p_files" / "1791460000123456").write_text("var t = 1;", encoding="utf-8")
    cap = Capture(id="f-1/lander", stage=Stage.lander,
                  artefacts=[Artefact(id="f-1/lander/a0", type=ArtefactType.asset, path="p_files/1791460000123456")])
    got = _index(platform.extract(Incident(id="f-1", source="synthetic", captures=[cap]), cap, tmp_path))
    assert ("meta_pixel", "1791460000123456") not in got


def test_emoji_in_aio_names_serialises(tmp_path):
    from ftf.indicators._html import aio_config

    page = "<script>window.aio = JSON.parse('{\"campaign\":{\"name\":\"PL \\\\ud83d\\\\ude80 Camp\"}}');</script>"
    cfg = aio_config(page)
    assert cfg["campaign"]["name"] == "PL \U0001F680 Camp"
    (tmp_path / "e.html").write_text(page, encoding="utf-8")
    cap = Capture(id="e-1/lander", stage=Stage.lander,
                  artefacts=[Artefact(id="e-1/lander/a0", type=ArtefactType.html, path="e.html")])
    inc = Incident(id="e-1", source="synthetic", captures=[cap])
    extract_all(Dataset(name="t", incidents=[inc]), tmp_path).to_json()


def test_whatsapp_placeholders_and_encoded_query():
    from ftf.indicators._ids import norm_path

    hits = {(h.kind, h.value) for h in scan(
        'https://wa.me/447911123456 <input placeholder="+48 512 345 678"> <input placeholder="+48 603 111 222">')}
    assert ("contact_phone", "+447911123456") in hits
    assert not any(k == "contact_phone" and v in ("+48512345678", "+48603111222") for k, v in hits)
    assert norm_path("/checkout.php%3Fsrc%3Dfb") == "/checkout.php"


def test_redact_catches_encoded_and_json_tokens():
    from ftf.indicators import redact

    tok = "EAAB" + "q9Wz" * 8
    cases = [
        f"https://a.example.test/?x=1&access_token={tok}&y=2",
        f"https%3A%2F%2Fa.example.test%2F%3Faccess_token%3D{tok}%26y%3D2",
        f"u=https%3A%2F%2Fa.example.test%2F%3Fk%3D{tok}",
        f"u=a.example.test%2F%3Fx%3D1%26{tok}",
        f'{{"access_token": "{tok}", "pixel_id": "{PIXEL}"}}',
        f'{{"Access_Token":"{tok}"}}',
        f"bare {tok} end",
    ]
    for s in cases:
        r = redact(s)
        assert "q9Wz" not in r, r
        assert "REDACTED" in r
    assert redact(f"x=1&y=2 {PIXEL}") == f"x=1&y=2 {PIXEL}"
    assert "y%3D2" in redact(cases[1])


def test_strip_tokens_encoded_forms():
    tok = "EAAB" + "q9Wz" * 8
    for s in (f"%3Faccess_token%3D{tok}%26a%3D1", f'{{"access_token":"{tok}"}}', f"x%3D{tok}", f"%26{tok}"):
        assert "q9Wz" not in strip_tokens(s)


def test_redact_and_strip_escaped_and_app_tokens():
    from ftf.indicators import redact

    tok = "EAAB" + "q9Wz" * 8
    secret = "SeCrEt" + "q9Wz" * 4
    leaky = [
        f"https://g.example.test/x?access_token=123456%7C{secret}&y=1",  # app token id|secret, encoded "|"
        f'JSON.parse("{{\\"u\\":\\"https://x.example.test/?access_token\\u003d{tok}\\"}}")',
        f"u%3Dhttps%253A%252F%252Fa.example.test%252F%253Faccess_token%253D{tok}",
        f"u=https%3A%2F%2Fa.example.test%2F{tok}",
        f'{{\\"access_token\\":\\"123|{secret}\\"}}',
        f"data=%7B%22access_token%22%3A%22123%7C{secret}%22%7D",
    ]
    for s in leaky:
        assert "q9Wz" not in redact(s), s
        assert "q9Wz" not in strip_tokens(s), s
    assert "y=1" in redact(leaky[0])
    assert "q9Wz" not in strip_tokens(f"https://x.example.test/cb#access_token=ya29.{secret}&state=1")
