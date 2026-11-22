#!/usr/bin/env python3
"""Fail if any organiser value appears in the repository or in its commit messages (HACK-33).

The organiser dataset (domains, hosts, URLs, id-like indicator values and page titles of the
challenge cases) exists only on the workbench VM, so this check runs there, not on the laptop:

    # laptop: write the commit messages once (git only lists them, no content is processed)
    git log --all --format=%B > "$TMPDIR/commit-messages.txt"
    gcloud compute scp "$TMPDIR/commit-messages.txt" ftf-workbench:/work/<slug>/ --project follow-the-fake \
      --zone europe-central2-a --tunnel-through-iap --quiet
    # VM: sync the worktree first (scripts/vm.sh run -- true), then:
    scripts/vm.sh run -- python3 -I scripts/check_no_organiser_values.py \
      --dataset /data/derived/ingest/organiser/dataset.json --root . \
      --messages /work/<slug>/commit-messages.txt

Candidates shorter than 6 characters and values of public hosts (the allowlist below) are ignored.
A hit prints only `file:line`, the category of the value and the first 12 hex digits of the
SHA-256 of the matched text; the value itself is never printed. Exit 0: no hit. Exit 1: at least
one hit. Exit 2: bad arguments or a missing dataset. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

MIN_LENGTH = 6
# Skipped at any depth (never tracked) and only at the root (local data and environments).
SKIP_ANY_DEPTH = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})
SKIP_AT_ROOT = frozenset({"data", "venv", ".venv"})
# One random key per run: digests compare equal within a run, but a guessed value cannot be
# confirmed against a digest printed in a log or a ticket later.
RUN_KEY = os.urandom(16)
# Public services and brands (a host matches the name and its subdomains). Chosen from the
# third-party services that public pages load and from the brands that scams impersonate; none
# of them is an organiser identifier. Keep this list short and public.
PUBLIC_HOSTS = frozenset({
    "google.com", "google.pl", "googleapis.com", "gstatic.com", "googleusercontent.com",
    "googletagmanager.com", "google-analytics.com", "googleadservices.com", "googlesyndication.com",
    "doubleclick.net", "facebook.com", "facebook.net", "fb.com", "fbcdn.net", "fbsbx.com", "meta.com",
    "cert.pl", "example.com", "example.org", "example.net", "w3.org", "cloudflare.com",
    "cloudflareinsights.com", "jsdelivr.net", "unpkg.com", "jquery.com", "bootstrapcdn.com",
    "fontawesome.com", "tailwindcss.com", "github.com", "githubusercontent.com", "youtube.com",
    "ytimg.com", "twitter.com", "twimg.com", "tiktok.com", "snapchat.com", "linkedin.com", "licdn.com",
    "yandex.ru", "yandex.net", "vuejs.org", "npmjs.com", "fpjs.dev", "npms.io", "python.org",
    "pypi.org", "linear.app", "anthropic.com", "docker.com", "debian.org", "ubuntu.com", "apache.org",
    "gnu.org", "mozilla.org",
    "ads-twitter.com",
    "bing.com",
    "clarity.ms",
    "quora.com",
    "taboola.com",
})
# Not organiser infrastructure: file names that look like domains, reserved test names.
FILE_EXTENSIONS = frozenset({
    "php", "html", "htm", "asp", "aspx", "jsp", "js", "mjs", "css", "json", "txt", "xml", "png", "jpg",
    "jpeg", "gif", "svg", "webp", "ico", "pdf", "zip", "gz", "tar", "md", "py", "csv", "woff", "woff2",
    "ttf", "otf", "eot", "mp4", "mov", "webm", "log", "sh", "ts", "yml", "yaml", "toml", "lock", "map",
    "pobrane", "har", "mhtml", "bak", "tmp",
})
RESERVED_TLDS = frozenset({"test", "example", "invalid", "local", "localhost", "arpa", "internal"})
ID_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9_.:\-]{5,}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
DIMENSION_RE = re.compile(r"^\d+x\d+$")
VERSION_RE = re.compile(r"^v?\d+(\.\d+)+$")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
DOMAIN_RE = re.compile(r"(?<![\w@/])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?![\w])", re.I)
TITLE_KIND_RE = re.compile(r"(title|name|caption|text|label)$|^page_title$")
TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9_.:\-]+")


def _public(host: str) -> bool:
    """True if the host, or one of its parent domains, is in PUBLIC_HOSTS (subdomains match)."""
    labels = host.lower().split(".")
    return any(".".join(labels[i:]) in PUBLIC_HOSTS for i in range(len(labels)))


def _real_domain(name: str) -> bool:
    return name.rsplit(".", 1)[-1] not in FILE_EXTENSIONS | RESERVED_TLDS


def _id_like(value: str, layer: str) -> bool:
    """A pixel, account, tracker or asset id: mixed characters, not a date, size, version or domain."""
    if layer == "collector" or not ID_RE.match(value):
        return False
    if DATE_RE.match(value) or DIMENSION_RE.match(value) or VERSION_RE.match(value):
        return False
    if DOMAIN_RE.fullmatch(value):
        return False
    return len(value) >= 12 if value.isdigit() else True


def _values(dataset: dict) -> list[tuple[str, str, str]]:
    """(kind, layer, value) of every indicator with a string value."""
    out = []
    for inc in dataset.get("incidents", []):
        for ind in inc.get("indicators", []):
            value = ind.get("value")
            if isinstance(value, str):
                out.append((ind.get("kind") or "", ind.get("layer") or "", value))
    return out


def extract(dataset: dict) -> dict[str, set[str]]:
    """Candidate strings by category: url, host, id, title. Lower-cased; short values dropped."""
    found: dict[str, set[str]] = {"url": set(), "host": set(), "id": set(), "title": set()}
    for kind, layer, value in _values(dataset):
        for url in URL_RE.findall(value):
            found["url"].add(url.lower())
            host = (urlsplit(url).hostname or "").lower()
            if host and _real_domain(host):
                found["host"].add(host)
            if host and _public(host):
                # Only the host is exempt: an id-like token in the path or query is still checked.
                parts = urlsplit(url)
                for token in TOKEN_SPLIT_RE.split(f"{parts.path} {parts.query}"):
                    stem = token.rsplit(".", 1)[0] if token.rsplit(".", 1)[-1] in FILE_EXTENSIONS else token
                    if _id_like(stem, layer):
                        found["id"].add(stem.lower())
        for dom in DOMAIN_RE.findall(value):
            if _real_domain(dom.lower()):
                found["host"].add(dom.lower())
        if _id_like(value, layer):
            found["id"].add(value.lower())
        if TITLE_KIND_RE.search(kind):
            found["title"].add(value.lower())
    for cat in found:
        found[cat] = {v for v in found[cat] if len(v) >= MIN_LENGTH}
    found["host"] = {h for h in found["host"] if not _public(h)}
    found["url"] = {u for u in found["url"] if not _public((urlsplit(u).hostname or "").lower())}
    found["id"] = {v for v in found["id"] if v not in PUBLIC_HOSTS}
    return found


def _needles(found: dict[str, set[str]]) -> dict[str, str]:
    """Map each needle (lower case, and its defanged form for hosts and URLs) to its category."""
    needles: dict[str, str] = {}
    for cat, values in found.items():
        for v in values:
            needles.setdefault(v, cat)
            if cat in ("host", "url"):
                needles.setdefault(v.replace(".", "[.]"), cat)
    return needles


def _scan_text(text: str, pattern: re.Pattern, needles: dict[str, str]):
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in pattern.finditer(line.lower()):
            yield lineno, needles[m.group(0)], m.group(0)


def _digest(matched: str) -> str:
    return hmac.new(RUN_KEY, matched.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if not path.is_file():
            continue
        if any(part in SKIP_ANY_DEPTH for part in parts) or parts[0] in SKIP_AT_ROOT:
            continue
        yield path


def check(dataset_path: Path, root: Path, messages: Path | None) -> int:
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"check_no_organiser_values: cannot read the dataset: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    found = extract(dataset)
    needles = _needles(found)
    if not needles:
        print("check_no_organiser_values: no candidates extracted; refusing to pass an empty check", file=sys.stderr)
        return 2
    pattern = re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)))
    print(
        "candidates: "
        + ", ".join(f"{cat} {len(found[cat])}" for cat in ("url", "host", "id", "title"))
        + f"; needles incl. defanged forms {len(needles)}"
    )
    hits = 0
    files = 0
    for path in _files(root):
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        files += 1
        for lineno, cat, matched in _scan_text(data.decode("utf-8", "replace"), pattern, needles):
            hits += 1
            digest = _digest(matched)
            print(f"{path.relative_to(root)}:{lineno}: organiser {cat} (digest {digest})")
    print(f"tracked files scanned: {files}")
    if messages is not None:
        text = messages.read_text(encoding="utf-8", errors="replace")
        for lineno, cat, matched in _scan_text(text, pattern, needles):
            hits += 1
            digest = _digest(matched)
            print(f"commit-messages:{lineno}: organiser {cat} (digest {digest})")
        print("commit messages scanned: yes")
    else:
        print("commit messages scanned: no (no --messages file given)")
    if hits:
        print(f"FAIL: {hits} hit(s)")
        return 1
    print("PASS: no organiser values found")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dataset", type=Path, required=True, help="organiser contract dataset.json (on the VM)")
    parser.add_argument("--root", type=Path, default=Path("."), help="repository tree to scan (default: .)")
    parser.add_argument("--messages", type=Path, help="file with the commit messages since the root")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"check_no_organiser_values: not a directory: {args.root}", file=sys.stderr)
        return 2
    return check(args.dataset, args.root, args.messages)


if __name__ == "__main__":
    raise SystemExit(main())
