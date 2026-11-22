#!/usr/bin/env python3
"""Fail if private data, secrets or raw challenge files reach tracked files or commit messages.

Scam infrastructure (domains, URLs, tracker, account, tenant and pixel ids) and the names of public
figures and brands are public and may be in git (owner, 2026-11-22 08:10). The check fails only on:

1. personal data of private individuals: e-mail addresses and phone numbers, including the ones in
   the organiser dataset (their digits are searched for; the value is never printed);
2. secrets: private keys, cloud and forge tokens, password or API key assignments;
3. raw challenge files committed as files: HAR, MHTML, WARC, PDF, screenshots and HTML dumps, by
   extension or by content (RAW_ALLOWED lists invented fixtures).

The dataset exists only on the workbench VM, so the check runs there:

    scripts/vm.sh run -- python3 -I scripts/check_no_private_data.py \
      --dataset /data/derived/ingest/organiser/dataset.json --root . [--messages FILE]

A hit prints `file:line: class (digest)` where class is personal, secret or raw; the matched text is
never printed. Exit 0: no hit. Exit 1: at least one hit. Exit 2: bad arguments or a missing dataset.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path

# One random key per run: digests compare equal within a run only.
RUN_KEY = os.urandom(16)
SKIP_ANY_DEPTH = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})
SKIP_AT_ROOT = frozenset({"data", "venv", ".venv"})
# Reserved names and domains are placeholders, never personal data.
RESERVED_DOMAINS = (
    "example", "test", "invalid", "local", "example.com", "example.org", "example.net", "noreply.github.com",
)
# Invented fixtures that look like captured pages (path -> reason). Nothing else may be committed raw.
RAW_ALLOWED = {"tests/scanner/fixtures/meta_ssr.html": "invented SSR fixture"}
# Test files whose e-mail and phone literals are invented (sequential digits, reserved or made-up
# domains); their secrets are still checked. Path -> reason.
PERSONAL_ALLOWED = {
    "tests/indicators/test_platform.py": "invented phone and e-mail literals in normaliser tests",
    "tests/enrich/test_enrich_sources.py": "invented e-mail literal in certificate-parsing test",
}
# Test files whose generic credential literals (assignments, URLs) are invented. Private keys and
# cloud or forge tokens are still checked everywhere. Path -> reason.
SECRET_ALLOWED = {
    "tests/indicators/test_domains.py": "invented access-token literal in URL-stripping test",
    "tests/indicators/test_kit.py": "invented access-token literal in URL-stripping test",
    "tests/fetch/test_guard_upstream.py": "invented proxy credential in guard test",
}
RAW_EXTENSIONS = {
    ".har": "HAR", ".mhtml": "MHTML", ".mht": "MHTML", ".warc": "WARC", ".pdf": "PDF",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".bmp": "image", ".html": "HTML", ".htm": "HTML",
    ".zip": "archive", ".tar": "archive", ".tgz": "archive", ".gz": "archive", ".7z": "archive",
    ".rar": "archive", ".mp4": "video", ".webm": "video", ".mov": "video",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", re.I)
PHONE_RE = re.compile(r"(?<![\w.])(?:\+48[ \-]?\d{9}|(?:\+48[ \-]?)?\d{3}[ \-]\d{3}[ \-]\d{3})(?![\w])")
SECRET_RES = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b|\bglpat-[A-Za-z0-9_\-]{20,}\b"),
)
# Generic credential shapes; a placeholder (<name>, $(command), {name}) is not a value.
SECRET_GENERIC_RES = (
    re.compile(
        r"""(?i)[\w\-]*(?:password|passwd|secret|token|api[_-]?key)[\w\-]*["']?\s*[:=]\s*["'](?![$<{])[^"'\s]{8,}["']"""
    ),
    re.compile(r"""[a-z][a-z0-9+.\-]*://(?![<{$])[^/\s:@'"]+:(?![<{$])(?!password@|pass@|secret@|changeme@)[^/\s@'"]{4,}@""", re.I),
)


def _digest(matched: str) -> str:
    return hmac.new(RUN_KEY, matched.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def _reserved(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS)


def _dataset_contacts(dataset: dict) -> tuple[set[str], set[str]]:
    """E-mail addresses and nine-digit phone numbers (lower case, digits only) of the dataset values."""
    emails: set[str] = set()
    phones: set[str] = set()
    for inc in dataset.get("incidents", []):
        for ind in inc.get("indicators", []):
            value = ind.get("value")
            if not isinstance(value, str):
                continue
            emails |= {m.lower() for m in EMAIL_RE.findall(value) if not _reserved(m)}
            for run in re.findall(r"\+?\d[\d \-]{6,}\d", value):
                digits = re.sub(r"\D", "", run)
                if re.fullmatch(r"(?:0{1,2}48|48)?\d{9}|0\d{9}", digits):
                    phones.add(digits[-9:])
    return emails, phones


def _rules(dataset: dict) -> tuple[list[tuple[str, re.Pattern]], int, int]:
    emails, phones = _dataset_contacts(dataset)
    rules: list[tuple[str, re.Pattern]] = [("personal-generic", EMAIL_RE), ("personal-generic", PHONE_RE)]
    rules += [("secret", pattern) for pattern in SECRET_RES]
    rules += [("secret-generic", pattern) for pattern in SECRET_GENERIC_RES]
    if emails or phones:
        parts = [re.escape(e) for e in sorted(emails)] + [r"[ \-]?".join(p) for p in sorted(phones)]
        rules.append(("personal-dataset", re.compile(r"(?<![\w@])(?:" + "|".join(parts) + r")(?!\w)", re.I)))
    return rules, len(emails), len(phones)


def _line_hits(line: str, rules):
    for cls, pattern in rules:
        for m in pattern.finditer(line):
            if cls.startswith("personal") and "@" in m.group(0) and _reserved(m.group(0)):
                continue
            yield cls.split("-")[0], m.group(0)


def _raw_type(path: Path, data: bytes) -> str | None:
    """Type name if the file is a captured page or a challenge screenshot/document, by name or content."""
    kind = RAW_EXTENSIONS.get(path.suffix.lower())
    if kind:
        return kind
    head = data[:8192]
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    ):
        return "image"
    if head.startswith((b"MIME-Version:", b"Snapshot-Content-Location:")):
        return "MHTML"
    if head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "HTML"
    if b'"entries"' in data and b'"log"' in data:
        try:
            log = json.loads(data).get("log", {})
        except (ValueError, AttributeError):
            return None
        if isinstance(log, dict) and "entries" in log:
            return "HAR"
    return None


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
        print(f"check_no_private_data: cannot read the dataset: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    values = [
        ind.get("value")
        for inc in dataset.get("incidents", [])
        for ind in inc.get("indicators", [])
        if isinstance(ind.get("value"), str)
    ]
    if not values:
        print("check_no_private_data: the dataset has no indicator values; refusing to pass", file=sys.stderr)
        return 2
    rules, n_emails, n_phones = _rules(dataset)
    print(f"dataset contacts: {n_emails} e-mail, {n_phones} phone number(s)")
    hits = 0
    files = 0
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        kind = _raw_type(path, data)
        if kind and rel not in RAW_ALLOWED:
            hits += 1
            print(f"{rel}:1: raw (digest {_digest(rel + kind)})")
        if b"\0" in data[:8192]:
            continue
        files += 1
        file_rules = rules
        if rel in PERSONAL_ALLOWED:
            file_rules = [r for r in file_rules if r[0] != "personal-generic"]
        if rel in SECRET_ALLOWED:
            file_rules = [r for r in file_rules if r[0] != "secret-generic"]
        for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            for cls, matched in _line_hits(line, file_rules):
                hits += 1
                print(f"{rel}:{lineno}: {cls} (digest {_digest(matched)})")
    print(f"files scanned: {files} (walked tree, excluding ignored build and data folders)")
    if messages is not None:
        text = messages.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for cls, matched in _line_hits(line, rules):
                hits += 1
                print(f"commit-messages:{lineno}: {cls} (digest {_digest(matched)})")
        print("commit messages scanned: yes")
    else:
        print("commit messages scanned: no (no --messages file given)")
    if hits:
        print(f"FAIL: {hits} hit(s)")
        return 1
    print("PASS: no private data, secrets or raw challenge files found")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dataset", type=Path, required=True, help="organiser contract dataset.json (on the VM)")
    parser.add_argument("--root", type=Path, default=Path("."), help="repository tree to scan (default: .)")
    parser.add_argument("--messages", type=Path, help="file with the commit messages since the root")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"check_no_private_data: not a directory: {args.root}", file=sys.stderr)
        return 2
    return check(args.dataset, args.root, args.messages)


if __name__ == "__main__":
    raise SystemExit(main())
