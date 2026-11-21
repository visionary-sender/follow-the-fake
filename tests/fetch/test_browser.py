"""End-to-end fetcher tests: headless Chromium against the local test server only.

Skipped when Playwright's Chromium cannot start here (then they run on the sandbox VM:
`scripts/sandbox.sh run -- python -m pytest ...` after `sandbox.sh sync`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from ftf.contract import ArtefactType, Dataset, Incident, Stage
from ftf.fetch import capture as cap
from ftf.fetch import classify as cl
from ftf.fetch.browser import FetchConfig, fetch_url
from ftf.fetch.runner import fetch_isolated



def _chromium_works() -> str | None:
    import os

    if os.geteuid() == 0:
        return "running as root"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True, chromium_sandbox=True).close()
    except Exception as e:  # noqa: BLE001
        return str(e).splitlines()[0][:200]
    return None


_why = _chromium_works()
pytestmark = pytest.mark.skipif(_why is not None, reason=f"Chromium unavailable: {_why}")


@pytest.fixture
def cfg(server):
    return FetchConfig(nav_timeout_s=10, settle_s=2, total_timeout_s=40,
                       allow_networks=["127.0.0.1/32"], allow_ports=[server.port, 80, 443])


def _check_capture(out: Path, m: dict) -> Incident:
    """The manifest holds a valid Incident whose artefacts exist with matching hashes."""
    inc = Incident.model_validate(m["incident"])
    on_disk = json.loads((out / m["capture_id"] / cap.MANIFEST).read_text())
    assert on_disk["incident"] == m["incident"]
    for a in inc.captures[0].artefacts:
        p = out / a.path
        assert p.is_file() and p.stat().st_size == a.size
        assert hashlib.sha256(p.read_bytes()).hexdigest() == a.sha256
    files = {p.name for p in (out / m["capture_id"]).iterdir()}
    assert files <= {cap.URL_TXT, cap.HAR, cap.HTML, cap.SCREENSHOT, cap.MANIFEST}
    return inc


def test_live_page(server, cfg, tmp_path):
    m = fetch_url(server.url("/ok"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE, m["reason"]
    inc = _check_capture(tmp_path, m)
    assert {a.type for a in inc.captures[0].artefacts} == {ArtefactType.url, ArtefactType.har, ArtefactType.html, ArtefactType.screenshot}
    assert inc.captures[0].missing == []
    har = json.loads((tmp_path / m["capture_id"] / cap.HAR).read_text())
    assert any(e["request"]["url"] == server.url("/ok") for e in har["log"]["entries"])
    assert "Breaking" in (tmp_path / m["capture_id"] / cap.HTML).read_text()
    assert (tmp_path / m["capture_id"] / cap.SCREENSHOT).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "HeadlessChrome" not in m["browser"]["user_agent"]


def test_http_redirect_chain(server, cfg, tmp_path):
    m = fetch_url(server.url("/redirect"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE
    assert [(h["url"], h["status"]) for h in m["redirect_chain"]] == [
        (server.url("/redirect"), 302), (server.url("/r2"), 301), (server.url("/ok"), 200)]
    assert m["final_url"] == server.url("/ok")
    urls = (tmp_path / m["capture_id"] / cap.URL_TXT).read_text().split()
    assert urls == [server.url("/redirect"), server.url("/r2"), server.url("/ok")]


def test_js_redirect_is_followed(server, cfg, tmp_path):
    m = fetch_url(server.url("/js-redirect"), tmp_path, cfg)
    assert m["final_url"] == server.url("/ok")
    assert [h["url"] for h in m["redirect_chain"]][-1] == server.url("/ok")


def test_forms_are_never_submitted(server, cfg, tmp_path):
    m = fetch_url(server.url("/form"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE
    assert "POST" not in server.methods()
    assert not {"/submitted", "/submitted-get", "/beacon", "/beacon2"} & server.paths()
    assert any(b["method"] == "POST" for b in m["blocked_requests"])


def test_metadata_and_private_addresses_unreachable(server, cfg, tmp_path):
    before = len(server.hits)
    m = fetch_url(server.url("/metadata"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE
    events = {(e["host"], e["outcome"]) for e in m["guard_events"]}
    assert ("169.254.169.254", "blocked_address") in events
    assert ("metadata.google.internal", "blocked_name") in events
    assert ("localhost", "blocked_name") in events
    assert ("10.0.0.1", "blocked_address") in events
    assert "/secret" not in {p for _, p in server.hits[before:]}
    assert not any("passwd" in (h["url"]) for h in m["redirect_chain"])


def test_redirect_to_metadata_is_blocked(server, cfg, tmp_path):
    m = fetch_url(server.url("/redirect-metadata"), tmp_path, cfg)
    assert m["outcome"] == cl.BLOCKED_ADDRESS, m["reason"]
    assert "169.254.169.254" in m["reason"]
    inc = _check_capture(tmp_path, m)
    assert {x.modality for x in inc.captures[0].missing} == {ArtefactType.html, ArtefactType.screenshot}


def test_redirect_to_file_is_not_followed(server, cfg, tmp_path):
    m = fetch_url(server.url("/redirect-file"), tmp_path, cfg)
    assert m["outcome"] in cl.DEAD
    assert not (tmp_path / m["capture_id"] / cap.HTML).exists()


def test_dead_host(cfg, tmp_path):
    m = fetch_url("http://nonexistent.invalid/landing", tmp_path, cfg)
    assert m["outcome"] == cl.DNS_FAILURE, m["reason"]
    assert m["category"] == "dead"
    inc = _check_capture(tmp_path, m)
    assert all(x.reason.startswith("dns_failure") for x in inc.captures[0].missing)


def test_dead_host_https(cfg, tmp_path):
    m = fetch_url("https://nonexistent.invalid/", tmp_path, cfg)
    assert m["outcome"] == cl.DNS_FAILURE, m["reason"]


def test_timeout(server, tmp_path):
    cfg = FetchConfig(nav_timeout_s=2, settle_s=1, allow_networks=["127.0.0.1/32"], allow_ports=[server.port])
    m = fetch_url(server.url("/slow"), tmp_path, cfg)
    assert m["outcome"] == cl.TIMEOUT, m["reason"]
    _check_capture(tmp_path, m)


@pytest.mark.parametrize(
    "path,outcome,stage",
    [
        ("/cloaked", cl.DECOY, Stage.decoy),
        ("/parked", cl.DECOY, Stage.decoy),
        ("/botwall", cl.BOT_WALL, Stage.unknown),
        ("/empty", cl.EMPTY, Stage.unknown),
        ("/missing", cl.HTTP_ERROR, Stage.unknown),
    ],
)
def test_cloaked_and_error_pages(server, cfg, tmp_path, path, outcome, stage):
    m = fetch_url(server.url(path), tmp_path, cfg)
    assert m["outcome"] == outcome, m["reason"]
    inc = _check_capture(tmp_path, m)
    assert inc.captures[0].stage is stage
    assert inc.indicators[0].value.startswith(outcome)


def test_navigation_to_dead_host_after_load(server, cfg, tmp_path):
    m = fetch_url(server.url("/js-to-dead"), tmp_path, cfg)
    assert m["outcome"] == cl.DNS_FAILURE, m["reason"]
    assert "then navigated to https://nonexistent.invalid/lander" in m["reason"]
    assert not (tmp_path / m["capture_id"] / cap.HTML).exists()
    assert "chrome-error" not in json.dumps(m)
    har = json.loads((tmp_path / m["capture_id"] / cap.HAR).read_text())
    assert any(e["request"]["url"] == server.url("/js-to-dead") for e in har["log"]["entries"])


def test_cloaker_sending_bots_to_loopback_is_decoy(server, cfg, tmp_path):
    m = fetch_url(server.url("/js-to-loopback"), tmp_path, cfg)
    assert m["outcome"] == cl.DECOY, m["reason"]
    inc = _check_capture(tmp_path, m)
    assert inc.captures[0].stage is Stage.decoy
    assert not (tmp_path / m["capture_id"] / cap.HTML).exists()


def test_site_cannot_fake_a_guard_refusal(server, cfg, tmp_path):
    m = fetch_url(server.url("/spoof"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE, m["reason"]
    assert "Real page" in (tmp_path / m["capture_id"] / cap.HTML).read_text()


def test_captcha_lead_form_is_live(server, cfg, tmp_path):
    assert fetch_url(server.url("/captcha-lander"), tmp_path, cfg)["outcome"] == cl.LIVE


def test_library_entry_point(server, cfg, tmp_path):
    from ftf.fetch import fetch_url as lib_fetch
    from ftf.fetch import fetch_urls

    r = lib_fetch(server.url("/ok"), tmp_path, cfg)
    assert r.outcome == cl.LIVE and r.category == "live"
    assert r.capture_dir == tmp_path / r.capture_id and (r.capture_dir / cap.MANIFEST).exists()
    assert Incident.model_validate(r.manifest["incident"]).source == "live_fetch"
    rs = fetch_urls([server.url("/missing"), "http://nonexistent.invalid/"], tmp_path, cfg)
    assert [x.category for x in rs] == ["dead", "dead"]
    assert len(Dataset.from_json((tmp_path / "dataset.json").read_text()).incidents) == 3


def test_victim_pl_mobile_profile(server, cfg, tmp_path):
    from dataclasses import replace

    cfg = replace(cfg, profile="victim-pl-mobile", referer="https://l.facebook.com/", fbclid=True)
    m = fetch_url(server.url("/echo?x=1"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE, m["reason"]
    echo = (tmp_path / m["capture_id"] / cap.HTML).read_text()
    assert "/echo?x=1&amp;fbclid=IwAR" in echo
    assert "Android" in echo and "Mobile Safari" in echo and "HeadlessChrome" not in echo
    low = echo.lower()
    assert "referer: https://l.facebook.com/" in low
    assert "accept-language: pl-pl" in low
    p = m["profile"]
    assert p["name"] == "victim-pl-mobile" and p["is_mobile"] and p["viewport"]["width"] == 412
    assert p["referer"] == "https://l.facebook.com/" and "fbclid=IwAR" in p["visited_url"]
    assert m["requested_url"] == server.url("/echo?x=1")  # the item's URL, without our click id
    assert "fbclid" not in (tmp_path / m["capture_id"] / cap.URL_TXT).read_text().splitlines()[0]


def test_popups_are_closed(server, cfg, tmp_path):
    m = fetch_url(server.url("/popup"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE
    assert server.url("/ok") in m["popups_closed"]


def test_no_downloads(server, cfg, tmp_path):
    m = fetch_url(server.url("/download"), tmp_path, cfg)
    assert m["outcome"] == cl.DOWNLOAD, m["reason"]
    assert not list(tmp_path.rglob("payload.exe"))
    assert not (tmp_path / m["capture_id"] / cap.HTML).exists()
    _check_capture(tmp_path, m)


def test_mobile_screenshot_height_is_capped_in_css_pixels(server, cfg, tmp_path):
    import struct
    from dataclasses import replace

    cfg = replace(cfg, profile="victim-pl-mobile", max_screenshot_height=3000)
    m = fetch_url(server.url("/big"), tmp_path, cfg)
    png = (tmp_path / m["capture_id"] / cap.SCREENSHOT).read_bytes()
    width, height = struct.unpack(">II", png[16:24])
    assert height <= 3000 and width <= 412


def test_byte_limit(server, tmp_path):
    cfg = FetchConfig(nav_timeout_s=10, settle_s=1, max_bytes=200_000,
                      allow_networks=["127.0.0.1/32"], allow_ports=[server.port])
    m = fetch_url(server.url("/big"), tmp_path, cfg)
    assert m["bytes_received"] <= 200_000 + 65536
    assert any("byte limit" in n for n in m["notes"])


def test_isolated_runner_and_dataset(server, cfg, tmp_path):
    m = fetch_isolated(server.url("/ok"), tmp_path, cfg)
    assert m["outcome"] == cl.LIVE, m["reason"]
    ds = Dataset.from_json(cap.write_dataset(tmp_path).read_text())
    assert len(ds.incidents) == 1


def test_isolated_runner_wall_clock_kill(server, tmp_path):
    cfg = FetchConfig(nav_timeout_s=30, settle_s=1, total_timeout_s=4,
                      allow_networks=["127.0.0.1/32"], allow_ports=[server.port])
    import os

    m = fetch_isolated(server.url("/slow"), tmp_path, cfg)
    assert m["outcome"] == cl.TIMEOUT and "killed" in m["reason"]
    _check_capture(tmp_path, m)
    assert not (tmp_path / m["capture_id"] / cap.HAR).exists()  # never a half-written HAR
    # No browser process from the visit survives, and the caller did not become a subreaper.
    leftovers = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read()
            except OSError:
                continue
            if b"ftf-browser-" in cmd or b"ftf.fetch.runner" in cmd:
                leftovers.append(cmd[:120])
    assert not leftovers, leftovers
    import tempfile

    assert not [p for p in os.listdir(tempfile.gettempdir()) if p.startswith("ftf-visit-")]  # temp dir removed


def test_cli_default_policy_blocks_loopback(server, tmp_path, capsys):
    """Without the test-only allow list, the local server (127.0.0.1) is unreachable."""
    from ftf.cli import main

    before = len(server.hits)
    assert main(["fetch", "--out", str(tmp_path), "--timeout", "10", "--settle", "1", server.url("/ok")]) == 0
    out = capsys.readouterr().out
    assert "blocked_address" in out
    assert len(server.hits) == before
    assert (tmp_path / "dataset.json").exists()
