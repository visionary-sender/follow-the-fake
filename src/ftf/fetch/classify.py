"""Decide what a page visit produced: a live page, or why there is no usable page.

Pure functions over strings, so they are unit-tested without a browser. Content heuristics are
deliberately conservative: they only fire on well-known markers and say which one fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Outcomes. `live` is the only one that means "we saw the page the URL serves".
LIVE = "live"
DNS_FAILURE = "dns_failure"
BLOCKED_ADDRESS = "blocked_address"  # the guard refused the destination (private / metadata / port)
CONNECTION_FAILED = "connection_failed"
TIMEOUT = "timeout"
NAVIGATION_ERROR = "navigation_error"
HTTP_ERROR = "http_error"
BOT_WALL = "bot_wall"
EMPTY = "empty"
DECOY = "decoy"
SIZE_LIMIT = "size_limit"  # the page's byte budget ran out before the document arrived
INVALID_URL = "invalid_url"
FETCHER_ERROR = "fetcher_error"

DOWNLOAD = "download"  # the URL served a file download (refused, nothing saved), not a page
DEAD = frozenset({DNS_FAILURE, BLOCKED_ADDRESS, CONNECTION_FAILED, TIMEOUT, NAVIGATION_ERROR, SIZE_LIMIT, DOWNLOAD,
                  INVALID_URL, FETCHER_ERROR})
CLOAKED = frozenset({BOT_WALL, EMPTY, DECOY})
OUTCOMES = frozenset({LIVE, HTTP_ERROR}) | DEAD | CLOAKED



def category(outcome: str) -> str:
    """live | cloaked | dead. An HTTP error counts as dead (the error page is still saved)."""
    if outcome == LIVE:
        return "live"
    return "cloaked" if outcome in CLOAKED else "dead"


_GUARD_TO_OUTCOME = {
    "dns_failure": DNS_FAILURE,
    "blocked_name": BLOCKED_ADDRESS,
    "blocked_port": BLOCKED_ADDRESS,
    "blocked_address": BLOCKED_ADDRESS,
    "connect_failed": CONNECTION_FAILED,
    "budget_exceeded": SIZE_LIMIT,
}


def outcome_from_guard(guard_outcome: str) -> str | None:
    return _GUARD_TO_OUTCOME.get(guard_outcome)


def outcome_from_net_error(message: str) -> str:
    m = message.upper()
    if "DOWNLOAD IS STARTING" in m or "ERR_ABORTED" in m and "DOWNLOAD" in m:
        return DOWNLOAD
    if "ERR_NAME_NOT_RESOLVED" in m or "ERR_NAME_RESOLUTION_FAILED" in m:
        return DNS_FAILURE
    if "TIMEOUT" in m or "ERR_TIMED_OUT" in m:
        return TIMEOUT
    if any(s in m for s in ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
                            "ERR_ADDRESS_UNREACHABLE", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE",
                            "ERR_SSL", "ERR_CERT", "ERR_CONNECTION_TIMED_OUT")):
        return CONNECTION_FAILED
    return NAVIGATION_ERROR


# (marker regex, label). Matched case-insensitively against the HTML.
# Strong markers only appear on challenge or block pages.
BOT_WALL_MARKERS: list[tuple[str, str]] = [
    (r"<title>\s*just a moment\.\.\.\s*</title>", "cloudflare challenge"),
    (r"/cdn-cgi/challenge-platform/|cf-chl-|cf_chl_opt", "cloudflare challenge"),
    (r"<title>\s*attention required!\s*\|\s*cloudflare", "cloudflare block page"),
    (r"checking your browser before accessing", "browser check"),
    (r"<title>\s*ddos-guard\s*</title>|ddos-guard\.net/[a-z/]*check", "ddos-guard"),
    (r"/__rd_verify_", "js verification challenge"),
    (r"<title>\s*(access denied|403 forbidden|request blocked)\s*</title>", "access denied page"),
    (r"incapsula incident id", "imperva block page"),
]
# Weak markers also appear on real pages (a lead form with a captcha, an anti-bot script), so
# they count only on a page that is short or answered with a blocking status.
WEAK_BOT_WALL_MARKERS: list[tuple[str, str]] = [
    (r"class=\"g-recaptcha\"|google\.com/recaptcha/api|hcaptcha\.com/1/api\.js|class=\"h-captcha\"|challenges\.cloudflare\.com/turnstile", "captcha"),
    (r"px-captcha|captcha\.px-cdn\.net", "perimeterx challenge"),
    (r"captcha-delivery\.com|geo\.captcha-delivery", "datadome challenge"),
]
BLOCKING_STATUSES = frozenset({401, 403, 429, 503})
SHORT_PAGE_CHARS = 400

DECOY_MARKERS: list[tuple[str, str]] = [
    (r"this domain (name )?(is|may be) for sale|buy this domain|domain is parked|parked free|parkingcrew|sedoparking|bodis\.com|dan\.com/buy-domain|afternic", "parked domain"),
    (r"<title>\s*welcome to nginx!?\s*</title>", "default server page (nginx)"),
    (r"<title>\s*apache2 (debian|ubuntu) default page", "default server page (apache)"),
    (r"<title>\s*it works!?\s*</title>", "default server page"),
    (r"<title>\s*index of /", "directory listing"),
    (r"<title>\s*(account suspended|suspended domain|site not found|domain not configured|website is unavailable|this site can.t be reached)", "suspended or unconfigured host"),
    (r"<title>\s*(coming soon|under construction)\s*</title>", "placeholder page"),
    (r"this (account|website) has been suspended|hosting account (has been )?suspended", "suspended hosting account"),
]

# Landing on one of these after starting elsewhere is the classic cloaker "white page" move.
WELL_KNOWN_HOSTS = (
    "google.com", "google.pl", "youtube.com", "facebook.com", "instagram.com", "wikipedia.org",
    "yahoo.com", "amazon.com", "microsoft.com", "apple.com", "onet.pl", "wp.pl",
    "interia.pl", "allegro.pl", "msn.com", "tiktok.com", "x.com", "twitter.com",
)

_TAG = re.compile(r"<(script|style|noscript|template)\b.*?</\1\s*>|<[^>]+>", re.I | re.S)
_WS = re.compile(r"\s+")


def visible_text(html: str) -> str:
    """Rough visible text of an HTML page (tags, scripts and styles removed)."""
    return _WS.sub(" ", _TAG.sub(" ", html)).strip()


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _registrable_suffix_match(host: str, domains: tuple[str, ...]) -> str | None:
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


@dataclass
class Verdict:
    outcome: str
    reason: str


def classify_content(requested_url: str, final_url: str, status: int | None, html: str, page_text: str | None = None,
                     min_text_chars: int = 40) -> Verdict:
    """Classify a page that loaded (the browser got a document)."""
    low = html.lower()
    text = page_text if page_text is not None else visible_text(html)
    text = _WS.sub(" ", text).strip()
    weak_ok = len(text) < SHORT_PAGE_CHARS or status in BLOCKING_STATUSES
    for markers, applies in ((BOT_WALL_MARKERS, True), (WEAK_BOT_WALL_MARKERS, weak_ok)):
        if not applies:
            continue
        for pattern, label in markers:
            if re.search(pattern, low, re.S):
                return Verdict(BOT_WALL, f"{label} (status {status})" if status else label)
    if status is not None and status >= 400:
        return Verdict(HTTP_ERROR, f"HTTP {status}")
    req_host, final_host = _host(requested_url), _host(final_url)
    known = _registrable_suffix_match(final_host, WELL_KNOWN_HOSTS)
    if known and final_host != req_host and not _registrable_suffix_match(req_host, (known,)):
        return Verdict(DECOY, f"redirected off-site to well-known host {final_host}")
    for pattern, label in DECOY_MARKERS:
        if re.search(pattern, low, re.S):
            return Verdict(DECOY, label)
    if len(text) < min_text_chars and "<img" not in low and "<video" not in low and "<canvas" not in low and "<iframe" not in low:
        return Verdict(EMPTY, f"page has {len(text)} characters of visible text and no media")
    return Verdict(LIVE, "page loaded")
