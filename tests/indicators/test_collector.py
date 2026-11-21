"""Collector extractor over small synthetic captures (fake domains, made-up values)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from ftf.contract import COLLECTOR_KINDS, Artefact, Capture, Incident, Layer, Level, Observed
from ftf.indicators import collector

FP = "0123456789abcdef0123456789abcdef"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def make_fbclid(session: bytes) -> str:
    raw = b"igrd\x01\x00\x00\x00extn\x00aem\x01\x00\x00pdof\x00adid\x05\x06\x07\x08\x09\x0a\x0b\x0csrtc\x00app_id" + session
    return "Iw" + base64.urlsafe_b64encode(raw).decode().rstrip("=") + "_aem_ABCDEFGHIJKLMNOPQRSTUV"


SESSION = bytes(range(40, 80))


def har(entries: list[dict], pages: list[dict] | None = None) -> bytes:
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "test"}, "pages": pages or [], "entries": entries}}).encode()


def entry(url: str, t: str = "2025-01-02T01:11:00.000Z", headers=None, post=None, response=None) -> dict:
    req = {"method": "POST" if post else "GET", "url": url, "headers": headers or [{"name": "User-Agent", "value": UA}]}
    if post:
        req["postData"] = post
    return {"startedDateTime": t, "request": req, "response": response or {"status": 200, "content": {"mimeType": "text/plain"}}}


AIO_VISIT = {
    "landing": {"uuid": "11111111-1111-4111-8111-111111111111"},
    "visit": {
        "uuid": "01a10000-0000-7000-8000-000000000000",
        "fingerprint": FP,
        "ip_address": "192.0.2.10",
        "asn_organization": "Example Net",
        "location_city": "Testville",
        "location_postal_code": "00-001",
        "country_code": "PL",
        "useragent": UA,
        "created_at": "2025-01-02T01:10:55Z",
        "browser_window_width": "1000",
        "browser_window_height": "700",
        "fields": {"fb_pixel": "1234567890123456", "fbclid": make_fbclid(SESSION)},
    },
}


def aio_html() -> bytes:
    blob = json.dumps(AIO_VISIT).replace("'", "\\'")
    return (
        "<!DOCTYPE html>\n<!-- saved from url=(0030)https://landing.example.test/ -->\n<html><head>"
        f"<script>window.aio = JSON.parse('{blob}');</script></head>"
        '<body><img src="file:///C:/Users/analyst1/Desktop/x.png"></body></html>'
    ).encode()


@pytest.fixture
def capture_root(tmp_path: Path):
    d = tmp_path / "case"
    d.mkdir()
    post_form = {
        "mimeType": "application/x-www-form-urlencoded",
        "text": f"browser_time_zone=Etc%2FGMT-3&browser_window_width=1000&browser_window_height=700&fingerprint={FP}",
        "params": [
            {"name": "browser_time_zone", "value": "Etc%2FGMT-3"},
            {"name": "browser_window_width", "value": "1000"},
            {"name": "browser_window_height", "value": "700"},
            {"name": "fingerprint", "value": FP},
        ],
    }
    post_json = {"mimeType": "application/json", "text": json.dumps({"useragent": UA, "viewport": {"width": 1000, "height": 700}})}
    geo = {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps({"ip": "198.51.100.7", "city": "Testville", "org": "AS64500 Example"})}}
    entries = [
        entry("https://landing.example.test/?utm_visit=x&fbclid=" + make_fbclid(SESSION), t="2025-01-02T01:11:01.000Z",
              headers=[{"name": "User-Agent", "value": UA}, {"name": "x-client-data", "value": "CIa2yQEIo7bJAQ=="}]),
        entry("https://landing.example.test/api/v1/trigger/field/abc/", post=post_form),
        entry("https://landing.example.test/api/v1/agent/x/", post=post_json),
        entry("https://geo.example.test/json", response=geo),
        entry("https://www.facebook.com/tr/?id=1234567890123456&ev=PageView&sw=1920&sh=1080&fbp=fb.1.1760000000000.123456789"),
    ]
    (d / "page.har").write_bytes(har(entries, pages=[{"startedDateTime": "2025-01-02T01:10:59.000Z", "id": "page_1"}]))
    (d / "page.html").write_bytes(aio_html())
    (d / "Zrzut ekranu 2025-01-02 031405.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND\xaeB`\x82")
    mhtml = (
        "From: <Saved by Blink>\r\nSnapshot-Content-Location: https://landing.example.test/\r\n"
        "Subject: test\r\nDate: Wed, 01 Jan 2025 04:05:06 GMT\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; type="text/html"; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html\r\nContent-Location: https://landing.example.test/\r\n\r\n<html>hi</html>\r\n--B--\r\n"
    )
    (d / "page.mhtml").write_bytes(mhtml.encode())
    (d / "archive_info.json").write_text(json.dumps({"url": "https://landing.example.test/", "timestamp_utc": "2025-01-01T04:05:06.4", "user_agent": UA}))
    cap = Capture(
        id="c1:s1",
        stage="lander",
        captured_on="2025-01-02",
        artefacts=[
            Artefact(id="c1:s1:har", type="har", path="case/page.har"),
            Artefact(id="c1:s1:html", type="html", path="case/page.html"),
            Artefact(id="c1:s1:shot", type="screenshot", path="case/Zrzut ekranu 2025-01-02 031405.png"),
            Artefact(id="c1:s1:mhtml", type="mhtml", path="case/page.mhtml"),
            Artefact(id="c1:s1:info", type="other", path="case/archive_info.json"),
            Artefact(id="c1:s1:gone", type="har", path="case/missing.har"),
        ],
    )
    inc = Incident(id="c1", source="test", captures=[cap])
    return inc, cap, tmp_path


def by_kind(inds):
    out: dict[str, list] = {}
    for i in inds:
        out.setdefault(i.kind, []).append(i)
    return out


def test_every_indicator_is_collector_and_valid(capture_root):
    inc, cap, root = capture_root
    inds = list(collector.extract(inc, cap, root))
    assert inds
    art_ids = {a.id for a in cap.artefacts}
    for i in inds:
        assert i.kind in COLLECTOR_KINDS
        assert i.layer is Layer.collector and i.level is Level.capture
        assert i.capture_id == cap.id
        assert i.provenance.artefact_id is None or i.provenance.artefact_id in art_ids
        assert i.extractor.startswith("collector@")
    # the result validates as part of an incident
    Incident.model_validate({**inc.model_dump(), "indicators": [i.model_dump() for i in inds]})


def test_each_collector_kind_is_found(capture_root):
    inc, cap, root = capture_root
    kinds = by_kind(collector.extract(inc, cap, root))
    expected = set(COLLECTOR_KINDS)
    assert expected <= kinds.keys(), expected - kinds.keys()


def test_aio_fingerprint_same_from_har_and_html(capture_root):
    inc, cap, root = capture_root
    fps = by_kind(collector.extract(inc, cap, root))["aio_fingerprint"]
    assert {i.value for i in fps} == {collector.pseudonymise("aio_fingerprint", FP)}
    assert {i.observed for i in fps} == {Observed.live, Observed.static}
    assert all(FP not in (i.value or "") for i in fps)


def test_viewport_and_screen(capture_root):
    inc, cap, root = capture_root
    kinds = by_kind(collector.extract(inc, cap, root))
    assert {i.value for i in kinds["viewport"]} == {"1000x700"}
    assert {i.value for i in kinds["screen_size"]} == {"1920x1080"}


def test_identifying_values_are_hashed(capture_root):
    inc, cap, root = capture_root
    inds = list(collector.extract(inc, cap, root))
    text = json.dumps([i.model_dump(mode="json") for i in inds])
    for raw in ("192.0.2.10", "198.51.100.7", "analyst1", "Testville", "00-001", FP, "CIa2yQEIo7bJAQ"):
        assert raw not in text
    kinds = by_kind(inds)
    assert {i.value for i in kinds["analyst_ip"]} == {
        collector.pseudonymise("analyst_ip", "192.0.2.10"),
        collector.pseudonymise("analyst_ip", "198.51.100.7"),
    }
    assert collector.pseudonymise("os_username", "analyst1") in {i.value for i in kinds["os_username"]}
    assert any(i.value.startswith("location_city=sha256:") for i in kinds["analyst_geo"])


def test_user_agent_and_times(capture_root):
    inc, cap, root = capture_root
    kinds = by_kind(collector.extract(inc, cap, root))
    assert {i.value for i in kinds["user_agent"]} == {UA}
    times = {i.value for i in kinds["capture_time"]}
    assert "2025-01-02T01:10:59.000Z" in times  # earliest HAR time (page)
    assert "2025-01-02T01:10:55Z" in times  # aio visit created_at
    assert "2025-01-02T03:14:05 local" in times  # screenshot file name
    assert "2025-01-01T04:05:06+00:00" in times  # MHTML Date
    assert "2025-01-01T04:05:06.4" in times  # archiver timestamp
    assert {"2025-01-02", "2025-01-01"} <= {i.value for i in kinds["capture_day"]}
    assert {i.value for i in kinds["screenshot_date"]} == {"2025-01-02"}


def test_fbclid_session_only_tail(capture_root):
    inc, cap, root = capture_root
    kinds = by_kind(collector.extract(inc, cap, root))
    assert {i.value for i in kinds["fbclid_session"]} == {collector.pseudonymise("fbclid_session", SESSION.hex())}
    assert collector.fbclid_session(make_fbclid(SESSION)) == SESSION
    assert collector.fbclid_session("IwAR" + "x" * 20) is None


def test_no_geo_without_ip_context(tmp_path: Path):
    (tmp_path / "a.har").write_bytes(har([entry("https://x.example.test/", response={
        "status": 200, "content": {"mimeType": "application/json", "text": json.dumps({"city": "Fictional", "country": "PL"})}})]))
    cap = Capture(id="c2:s1", stage="lander", artefacts=[Artefact(id="c2:a", type="har", path="a.har")])
    inc = Incident(id="c2", source="t", captures=[cap])
    kinds = by_kind(collector.extract(inc, cap, tmp_path))
    assert "analyst_geo" not in kinds


def test_percent_encoded_local_path_in_har(tmp_path: Path):
    url = "https://tags.example.test/collect?v=1&url=file%3A%2F%2F%2FC%3A%2FUsers%2Fanalyst2%2FDownloads%2Fp.html&tid=G-TEST"
    (tmp_path / "a.har").write_bytes(har([entry(url)]))
    cap = Capture(id="c4:s1", stage="lander", artefacts=[Artefact(id="c4:a", type="har", path="a.har")])
    inc = Incident(id="c4", source="t", captures=[cap])
    kinds = by_kind(collector.extract(inc, cap, tmp_path))
    assert {i.value for i in kinds["os_username"]} == {collector.pseudonymise("os_username", "analyst2")}
    assert {i.value for i in kinds["local_path"]} == {collector.pseudonymise("local_path", "file:///C:/Users/analyst2/Downloads/p.html")}


def test_garbage_files_do_not_crash(tmp_path: Path):
    (tmp_path / "bad.har").write_bytes(b"{not json")
    (tmp_path / "bad.html").write_bytes(b"\xff\xfe window.aio = JSON.parse('{broken'); ")
    (tmp_path / "bad.mhtml").write_bytes(b"\x00\x01garbage")
    cap = Capture(id="c3:s1", stage="lander", artefacts=[
        Artefact(id="c3:h", type="har", path="bad.har"),
        Artefact(id="c3:t", type="html", path="bad.html"),
        Artefact(id="c3:m", type="mhtml", path="bad.mhtml"),
    ])
    inc = Incident(id="c3", source="t", captures=[cap])
    assert list(collector.extract(inc, cap, tmp_path)) == []


def one_har_capture(tmp_path: Path, data: bytes, cid: str = "c5"):
    (tmp_path / "x.har").write_bytes(data)
    cap = Capture(id=f"{cid}:s1", stage="lander", artefacts=[Artefact(id=f"{cid}:a", type="har", path="x.har")])
    return Incident(id=cid, source="t", captures=[cap]), cap


@pytest.mark.parametrize(
    "payload",
    [
        har([entry("https://x.example.test/?sw=inf&sh=1080")]),
        har([entry("https://x.example.test/", post={"mimeType": "application/json", "text": '{"screen_width": Infinity, "screen_height": 1}'})]),
        har([entry("https://x.example.test/", post={"mimeType": "application/json", "text": "[" * 100_000})]),
        har([{"request": [], "response": "x"}, {"request": {"url": "https://x.example.test/", "postData": "a=b"}, "response": {"content": "z"}}]),
        json.dumps({"log": {"entries": 5, "pages": "x"}}).encode(),
    ],
)
def test_hostile_har_shapes_do_not_crash(tmp_path: Path, payload: bytes):
    inc, cap = one_har_capture(tmp_path, payload)
    inds = list(collector.extract(inc, cap, tmp_path))
    assert all(i.layer is Layer.collector for i in inds)
    assert "screen_size" not in {i.kind for i in inds}


def test_earliest_har_time_compares_instants(tmp_path: Path):
    payload = har([entry("https://x.example.test/a", t="2025-01-02T07:30:00Z"), entry("https://x.example.test/b", t="2025-01-02T08:00:00+02:00")])
    inc, cap = one_har_capture(tmp_path, payload)
    times = {i.value for i in collector.extract(inc, cap, tmp_path) if i.kind == "capture_time"}
    assert times == {"2025-01-02T08:00:00+02:00"}


def test_invalid_screenshot_name_date_is_ignored(tmp_path: Path):
    (tmp_path / "capture_12345678.png").write_bytes(b"")
    cap = Capture(id="c6:s1", stage="ad", artefacts=[Artefact(id="c6:a", type="ad_screenshot", path="capture_12345678.png")])
    inds = list(collector.extract(Incident(id="c6", source="t", captures=[cap]), cap, tmp_path))
    assert not [i for i in inds if i.kind in ("screenshot_date", "capture_day", "capture_time")]


def test_mhtml_date_without_zone_is_utc(tmp_path: Path):
    (tmp_path / "p.mhtml").write_bytes(b"From: <Saved by Blink>\r\nDate: Wed, 01 Jan 2025 04:05:06 -0000\r\nMIME-Version: 1.0\r\nContent-Type: text/html\r\n\r\n<html></html>\r\n")
    cap = Capture(id="c7:s1", stage="lander", artefacts=[Artefact(id="c7:a", type="mhtml", path="p.mhtml")])
    inds = list(collector.extract(Incident(id="c7", source="t", captures=[cap]), cap, tmp_path))
    assert {i.value for i in inds if i.kind == "capture_time"} == {"2025-01-01T04:05:06+00:00"}
