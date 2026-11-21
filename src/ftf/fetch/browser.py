"""Capture one URL in a locked-down headless Chromium.

Safety model (see the README, "Safe URL fetcher"):
- every request goes through `GuardProxy`, which refuses non-public destinations, including
  after redirects and DNS rebinding; WebRTC is limited to proxied traffic and QUIC is off;
- only http/https; only GET/HEAD/OPTIONS leave the browser, so nothing is ever submitted;
  `form.submit()`/`requestSubmit()` and submit events are neutralised; we never click;
- no downloads, no permissions, service workers blocked, popups closed at once;
- Chromium's own sandbox stays on and we refuse to run as root; the browser gets a minimal
  environment (no host variables, a throwaway HOME), so it never sees credentials;
- hard limits: navigation and settle time, bytes received, request count, HTML size and
  screenshot height. The caller (`runner.fetch_isolated`) adds a wall-clock kill.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ftf.fetch import capture as cap
from ftf.fetch import classify as cl
from ftf.fetch.guard import GuardProxy, host_allowed_by_name, parse_ip

ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Runs in every frame before page scripts: forms can never be submitted.
NO_SUBMIT_JS = """
(() => {
  const block = function () { return undefined; };
  try {
    Object.defineProperty(HTMLFormElement.prototype, 'submit', {value: block, writable: false, configurable: false});
    Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', {value: block, writable: false, configurable: false});
  } catch (e) {}
  window.addEventListener('submit', (e) => { e.preventDefault(); e.stopImmediatePropagation(); }, true);
})();
"""

CHROMIUM_ARGS = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-extensions",
    "--disable-breakpad",
    "--no-first-run",
    "--no-pings",
]


@dataclass
class FetchConfig:
    nav_timeout_s: float = 30.0
    settle_s: float = 6.0
    total_timeout_s: float | None = None  # wall-clock kill in the isolated runner; default nav + settle + 45
    max_bytes: int = 64 * 1024 * 1024
    max_requests: int = 1500
    max_html_bytes: int = 15 * 1024 * 1024
    max_screenshot_height: int = 12000
    viewport_width: int = 1366
    viewport_height: int = 900
    locale: str = "pl-PL"
    timezone_id: str = "Europe/Warsaw"
    user_agent: str | None = None  # default: a desktop Chrome UA matching the browser version
    # Visitor profile (see PROFILES). `referer` is sent with the first request; `fbclid` appends a
    # random Meta-style click id, as when the URL is opened from a Meta ad.
    profile: str = "default"
    referer: str | None = None
    fbclid: bool = False
    # Test-only: networks the guard lets through (the local test server on 127.0.0.1) and
    # extra ports. Never exposed on the command line.
    allow_networks: list[str] = field(default_factory=list)
    allow_ports: list[int] | None = None

    def __post_init__(self) -> None:
        if self.total_timeout_s is None:
            self.total_timeout_s = self.nav_timeout_s + self.settle_s + 45


MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Mobile Safari/537.36"
)

# Opt-in visitor profiles against scam cloaking. Cloakers show the scam only to visitors who look
# like targets (Polish, on a phone, arriving from a Meta ad) and a decoy to everyone else. A profile
# changes only what the browser presents; it never adds proxies or IP rotation, and the network
# guard and lockdown stay the same. The profile is collector data: recorded in capture.json, never
# linkable.
PROFILES: dict[str, dict] = {
    "default": {},
    "victim-pl-mobile": {
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": MOBILE_UA,
        "locale": "pl-PL",
        "timezone_id": "Europe/Warsaw",  # locale also sets Accept-Language: pl-PL
    },
}


def add_fbclid(url: str) -> str:
    """Append a random Meta-style click id (`fbclid=IwAR...`), keeping any existing query."""
    import secrets

    token = "IwAR" + secrets.token_urlsafe(48)[:58].replace("-", "_")
    base, sep, frag = url.partition("#")
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}fbclid={token}{sep}{frag}"


def profile_settings(config: "FetchConfig", major: str) -> dict:
    """Browser-context settings and the record for capture.json for the chosen profile."""
    if config.profile not in PROFILES:
        raise ValueError(f"unknown profile {config.profile!r}; choose from {sorted(PROFILES)}")
    p = PROFILES[config.profile]
    ua = config.user_agent or p.get("user_agent", "").format(major=major) or (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    context = {
        "user_agent": ua,
        "locale": p.get("locale", config.locale),
        "timezone_id": p.get("timezone_id", config.timezone_id),
        "viewport": p.get("viewport", {"width": config.viewport_width, "height": config.viewport_height}),
        "device_scale_factor": p.get("device_scale_factor", 1),
        "is_mobile": p.get("is_mobile", False),
        "has_touch": p.get("has_touch", False),
        "extra_http_headers": {},
    }
    if not config.user_agent:
        # Headless Chromium announces "HeadlessChrome" in its client hints; send the hints a
        # normal Chrome would, matching the user agent.
        context["extra_http_headers"].update({
            "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?1" if context["is_mobile"] else "?0",
            "sec-ch-ua-platform": '"Android"' if context["is_mobile"] else '"Windows"',
        })
    return context


class FetcherRefused(RuntimeError):
    """The fetcher will not run in this environment (e.g. as root)."""


def check_environment() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise FetcherRefused(
            "refusing to run Chromium as root (its sandbox needs an unprivileged user); "
            "run as a normal user, e.g. the container's default user"
        )


def validate_url(url: str) -> str | None:
    """Return an error message if `url` must not be fetched, else None."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as e:
        return f"malformed URL: {e}"
    if parts.scheme.lower() not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'!r} not allowed; only http and https"
    if not host:
        return "URL has no host"
    if parse_ip(host) is None and not host_allowed_by_name(host):
        return f"host {host!r} is an internal name"
    if any(c in url for c in "\r\n\t\x00"):
        return "URL contains control characters"
    return None


def _browser_env(home: str) -> dict[str, str]:
    """The only environment the browser process sees: no host variables, no credentials."""
    env = {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": home}
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    return env


def fetch_url(url: str, out_root: Path, config: FetchConfig | None = None, capture_id: str | None = None) -> dict:
    """Capture `url` into `out_root/<capture_id>/` and return the manifest (also written as capture.json)."""
    config = config or FetchConfig()
    started = time.time()
    capture_id = capture_id or cap.new_capture_id(url, started)
    cdir = Path(out_root) / capture_id
    cdir.mkdir(parents=True, exist_ok=True)
    rec = cap.Recorder(url=url, capture_dir=cdir, capture_id=capture_id, started=started, config=config)

    err = validate_url(url)
    if err is not None:
        return rec.finish(cl.INVALID_URL, err)
    if config.profile not in PROFILES:
        raise ValueError(f"unknown profile {config.profile!r}; choose from {sorted(PROFILES)}")
    if config.referer is not None and not config.referer.lower().startswith(("http://", "https://")):
        raise ValueError("referer must be an http(s) URL")
    check_environment()
    visit_url = add_fbclid(url) if config.fbclid else url

    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    import ipaddress

    allow = [ipaddress.ip_network(n) for n in config.allow_networks]
    deadline = started + config.nav_timeout_s + config.settle_s + 20

    def remaining_ms(cap_s: float) -> float:
        return max(500.0, min(cap_s, deadline - time.time()) * 1000)

    nav_error: str | None = None
    with GuardProxy(max_bytes=config.max_bytes, allow_networks=allow, allow_ports=config.allow_ports) as proxy, \
            tempfile.TemporaryDirectory(prefix="ftf-browser-") as home, sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            chromium_sandbox=True,
            args=[f"--proxy-server={proxy.url}", "--proxy-bypass-list=<-loopback>", *CHROMIUM_ARGS],
            env=_browser_env(home),
            downloads_path=os.path.join(home, "downloads"),
        )
        try:
            major = browser.version.split(".")[0]
            settings = profile_settings(config, major)
            context = browser.new_context(
                record_har_path=str(cdir / cap.HAR),
                record_har_content="embed",
                accept_downloads=False,
                service_workers="block",
                permissions=[],
                java_script_enabled=True,
                ignore_https_errors=True,
                **settings,
            )
            context.add_init_script(NO_SUBMIT_JS)
            rec.user_agent = settings["user_agent"]
            rec.browser_version = browser.version
            rec.viewport_width = settings["viewport"]["width"]
            rec.profile = {
                "name": config.profile,
                "user_agent": settings["user_agent"],
                "locale": settings["locale"],
                "timezone_id": settings["timezone_id"],
                "viewport": settings["viewport"],
                "is_mobile": settings["is_mobile"],
                "referer": config.referer,
                "visited_url": visit_url,
            }

            n_requests = 0

            def route(r) -> None:
                nonlocal n_requests
                req = r.request
                n_requests += 1
                scheme = req.url.split(":", 1)[0].lower()
                why = None
                if scheme not in ("http", "https"):
                    why = f"scheme {scheme} not allowed"
                elif req.method.upper() not in ALLOWED_METHODS:
                    why = f"method {req.method} not allowed (nothing is submitted)"
                elif n_requests > config.max_requests:
                    why = f"request limit {config.max_requests} reached"
                if why is None:
                    r.continue_()
                else:
                    rec.blocked(req.url, req.method, why)
                    r.abort("blockedbyclient")

            context.route("**/*", route)
            page = context.new_page()

            def on_page(p) -> None:
                if p is not page:
                    if p.url not in ("", "about:blank") and p.url not in rec.popups and len(rec.popups) < 100:
                        rec.popups.append(p.url)
                    try:
                        p.close()
                    except PwError:
                        pass

            def on_context_request(req) -> None:
                # A popup is closed before it navigates; its first request still names the target.
                try:
                    is_popup = req.is_navigation_request() and req.frame.page is not page
                except PwError:
                    return
                if is_popup and req.url not in rec.popups and len(rec.popups) < 100:
                    rec.popups.append(req.url)

            context.on("page", on_page)
            context.on("request", on_context_request)

            def on_request(req) -> None:
                if req.is_navigation_request() and req.frame == page.main_frame:
                    rec.last_nav_url = req.url

            def on_response(resp) -> None:
                req = resp.request
                if req.is_navigation_request() and req.frame == page.main_frame:
                    rec.hop(resp.url, resp.status, resp.headers.get("location"), resp.headers.get(proxy.header.lower()))

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(visit_url, wait_until="load", timeout=remaining_ms(config.nav_timeout_s),
                          referer=config.referer)
            except PwTimeout as e:
                nav_error = f"timeout: {str(e).splitlines()[0]}"
            except PwError as e:
                nav_error = str(e).splitlines()[0]
            got_document = any(not h.get("guard") and not 300 <= h["status"] < 400 for h in rec.chain)
            if nav_error is None or (nav_error.startswith("timeout") and got_document):
                try:
                    page.wait_for_load_state("networkidle", timeout=remaining_ms(config.settle_s))
                except PwError:
                    pass
            rec.final_url = page.url
            # A navigation after the page loaded can end on Chromium's error page or on a guard
            # refusal: neither is the site, so neither is saved as the page (the HAR keeps the
            # earlier document).
            rec.failed_after_load = got_document and (
                page.url.startswith("chrome-error://") or bool(rec.chain and rec.chain[-1].get("guard"))
            )
            # Only a real page is saved: not a download (navigation fails with "Download is
            # starting" and leaves about:blank), not an error page.
            real_page = page.url.startswith(("http://", "https://")) and (
                nav_error is None or cl.outcome_from_net_error(nav_error) != cl.DOWNLOAD)
            if got_document and real_page and not rec.failed_after_load:
                rec.capture_page(page, PwError)
            context.close()  # writes the HAR
        finally:
            browser.close()
        rec.guard = proxy

    return rec.finish_from_visit(nav_error)
