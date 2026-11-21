"""`ftf fetch`: capture URLs safely (single URLs or a batch from a list file)."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAILING = ".,;:!?)]}>"


def read_url_list(path: Path) -> list[str]:
    """URLs from a text file: every http(s) token, in order, without duplicates. Other text is ignored."""
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for u in _URL.findall(line):
            u = re.sub(r"(&gt;|&quot;|&#39;)+$", "", u)
            # Punctuation that closes a sentence or a bracket is not part of the URL, unless the
            # URL itself opened that bracket (e.g. Wikipedia-style paths).
            while u and u[-1] in _TRAILING and not (u[-1] == ")" and u.count("(") >= u.count(")")):
                u = u[:-1]
            if u and u not in urls:
                urls.append(u)
    return urls


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fetch",
        help="capture URLs in a locked-down headless browser (HAR, HTML, screenshot, redirects)",
        description="Capture each URL into <out>/<capture_id>/ (url.txt, page.har, page.html, screenshot.png, "
        "capture.json) and refresh <out>/dataset.json. Dead and cloaked pages are results, not errors.",
    )
    p.add_argument("urls", nargs="*", metavar="URL")
    p.add_argument("--list", type=Path, metavar="FILE", help="text file with URLs (batch mode)")
    p.add_argument("--out", type=Path, required=True, metavar="DIR", help="output directory")
    p.add_argument("--timeout", type=float, default=30.0, metavar="S", help="navigation timeout per page (default 30)")
    p.add_argument("--settle", type=float, default=6.0, metavar="S", help="extra wait for scripts and redirects (default 6)")
    p.add_argument("--max-mb", type=float, default=64.0, metavar="MB", help="bytes received per page (default 64)")
    p.add_argument("--profile", choices=["default", "victim-pl-mobile"], default="default",
                   help="visitor profile; victim-pl-mobile looks like a Polish phone user (against cloaking)")
    p.add_argument("--referer", metavar="URL", help="Referer for the first request, e.g. https://l.facebook.com/")
    p.add_argument("--fbclid", action="store_true", help="append a random fbclid, as when opened from a Meta ad")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from ftf.fetch import capture as cap
    from ftf.fetch.browser import FetchConfig, FetcherRefused, check_environment
    from ftf.fetch.runner import fetch_isolated

    urls = list(args.urls)
    if args.list is not None:
        urls += [u for u in read_url_list(args.list) if u not in urls]
    if not urls:
        print("ftf fetch: no URLs given (pass URLs or --list FILE)", file=sys.stderr)
        return 2
    try:
        check_environment()
    except FetcherRefused as e:
        print(f"ftf fetch: {e}", file=sys.stderr)
        return 3
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        print("ftf fetch: Playwright is not installed; run: pip install 'ftf[fetch]' && "
              "playwright install --with-deps --only-shell chromium", file=sys.stderr)
        return 3
    config = FetchConfig(
        nav_timeout_s=args.timeout,
        settle_s=args.settle,
        max_bytes=int(args.max_mb * 1024 * 1024),
        profile=args.profile,
        referer=args.referer,
        fbclid=args.fbclid,
    )
    if args.referer is not None and not args.referer.lower().startswith(("http://", "https://")):
        print("ftf fetch: --referer must be an http(s) URL", file=sys.stderr)
        return 2
    counts: Counter[str] = Counter()
    counts_errors = 0
    for i, url in enumerate(urls, 1):
        m = fetch_isolated(url, args.out, config)
        counts[m["category"]] += 1
        counts_errors += m["outcome"] == "fetcher_error"
        print(f"[{i}/{len(urls)}] {m['category']:<10} {m['outcome']:<17} {m['capture_id']}  {m['reason']}", flush=True)
    path = cap.write_dataset(args.out)
    summary = ", ".join(f"{k} {counts[k]}" for k in ("live", "cloaked", "dead") if counts[k])
    print(f"done: {len(urls)} URL(s): {summary}; dataset: {path}")
    if counts_errors:
        print(f"ftf fetch: the fetcher itself failed on {counts_errors} URL(s) (outcome fetcher_error); "
              "if all failed, check that Chromium is installed: playwright install --only-shell chromium", file=sys.stderr)
        return 1
    return 0
