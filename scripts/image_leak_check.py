#!/usr/bin/env python3
"""Fail if a built image contains organiser data, a campaign base, git metadata or secrets.

    python3 -I scripts/image_leak_check.py ftf:dev [--raw /data/raw] [--docker "sudo docker"]

Creates a stopped container from IMAGE, streams `docker export` (the full filesystem) and scans
every entry; the container is removed afterwards. Run it on the workbench VM, never on the
laptop. With --raw, files byte-identical to any file in the organiser folder and path components
equal to its case folder names are hits too. Exit 0: clean; 1: hits (listed); 2: usage error.

Rules:
* paths: `.git`, capture formats (.har, .mhtml, ...), campaign-base files (dataset.json,
  cache.json, build-report.json, vectors/), credential file names, anything under /data or
  /base, images or videos outside library trees (/usr, /etc, /opt/ms-playwright);
* contents (files up to 32 MB): private keys, service-account JSON and well-known token formats.

Standard library only. Hits print paths and the rule, never file contents.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Iterator

CAPTURE_SUFFIXES = {".har", ".mhtml", ".mht", ".eml", ".pcap", ".warc"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mp4", ".webm", ".mov", ".mkv", ".avi"}
BASE_NAMES = {"dataset.json", "cache.json", "build-report.json"}
SECRET_NAMES = {
    ".env", ".netrc", ".git-credentials", ".pypirc", ".envrc", "credentials.json",
    "application_default_credentials.json", "credentials.db", "access_tokens.db",
    "id_rsa", "id_ecdsa", "id_ed25519", "id_dsa",
}
# Library trees where images ship with packages (Streamlit/Plotly assets, fonts, Chromium).
MEDIA_OK_PREFIXES = ("usr/", "etc/", "opt/ms-playwright/")
DATA_DIRS = ("data/", "base/")
MAX_CONTENT = 32 * 1024 * 1024
# Copies are matched by hash only from this size up: empty and tiny files (`__init__.py`,
# `py.typed`, `/etc/hostname`) collide with trivial organiser files and say nothing.
MIN_HASH_SIZE = 512
SECRET_PATTERNS = [
    # A header alone is a string constant in TLS libraries (gnutls, gio); a key has a body.
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{40}")),
    ("service account", re.compile(rb'"type"\s*:\s*"service_account"')),
    ("github token", re.compile(rb"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}")),
    ("aws key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("google api key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("linear key", re.compile(rb"\blin_api_[A-Za-z0-9]{32,}")),
    ("anthropic/openai key", re.compile(rb"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}")),
    ("slack token", re.compile(rb"\bxox[abpr]-[A-Za-z0-9-]{20,}")),
]


@dataclass
class RawIndex:
    """Sizes and sha256 of every organiser file, plus case folder names (depth 1-2)."""

    hashes: dict[int, set[str]] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, root: Path) -> "RawIndex":
        idx = cls()
        for p in root.rglob("*"):
            rel = p.relative_to(root).parts
            if p.is_dir() and len(rel) <= 2 and len(p.name) >= 4:
                idx.names.add(p.name)
            elif p.is_file() and not p.is_symlink() and p.stat().st_size >= MIN_HASH_SIZE:
                idx.hashes.setdefault(p.stat().st_size, set()).add(_sha256(p.open("rb")))
        return idx


def _sha256(fh: IO[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def path_hits(name: str, raw: RawIndex | None) -> list[str]:
    path = PurePosixPath(_norm(name))
    parts = path.parts
    low = str(path).lower()
    base = path.name.lower()
    hits = []
    if ".git" in parts:
        hits.append("git metadata")
    if path.suffix.lower() in CAPTURE_SUFFIXES:
        hits.append("capture file")
    if base in BASE_NAMES or "vectors" in parts and path.suffix.lower() in {".npy", ".json"}:
        hits.append("campaign base file")
    if base in SECRET_NAMES or ".config/gcloud" in low or ".ssh/" in low:
        hits.append("credential file")
    if any(low.startswith(d) for d in DATA_DIRS):
        hits.append("file under a data volume")
    if path.suffix.lower() in MEDIA_SUFFIXES and not low.startswith(MEDIA_OK_PREFIXES):
        hits.append("media outside library trees")
    if raw is not None and raw.names.intersection(parts):
        hits.append("organiser case folder name")
    return hits


def content_hits(data: bytes) -> list[str]:
    return [label for label, rx in SECRET_PATTERNS if rx.search(data)]


def scan_tar(stream: IO[bytes], raw: RawIndex | None = None) -> tuple[int, list[tuple[str, str]]]:
    """(entries scanned, [(path, rule)]) for a tar stream of a filesystem."""
    hits: list[tuple[str, str]] = []
    n = 0
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for m in tar:
            n += 1
            name = m.name
            if m.isdir():
                if ".git" in PurePosixPath(_norm(name)).parts:
                    hits.append((name, "git metadata"))
                continue
            hits += [(name, h) for h in path_hits(name, raw)]
            if not m.isfile():
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            if m.size <= MAX_CONTENT:
                data = fh.read()
                hits += [(name, h) for h in content_hits(data)]
                if raw is not None and m.size in raw.hashes:
                    if hashlib.sha256(data).hexdigest() in raw.hashes[m.size]:
                        hits.append((name, "byte-identical to an organiser file"))
            elif raw is not None and m.size in raw.hashes and _sha256(fh) in raw.hashes[m.size]:
                hits.append((name, "byte-identical to an organiser file"))
    return n, hits


def _norm(name: str) -> str:
    name = name.lstrip("/")
    while name.startswith("./"):
        name = name[2:]
    return name


@contextmanager
def _export(image: str, docker: list[str]) -> Iterator[IO[bytes]]:
    """Filesystem tar stream of a stopped container made from `image`; the container is removed."""
    cid = subprocess.run([*docker, "create", image], check=True, capture_output=True, text=True).stdout.strip()
    try:
        proc = subprocess.Popen([*docker, "export", cid], stdout=subprocess.PIPE)
        try:
            yield proc.stdout
            while proc.stdout.read(1 << 20):  # drain trailing padding so export exits cleanly
                pass
        finally:
            proc.stdout.close()
            proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"docker export exited {proc.returncode}")
    finally:
        subprocess.run([*docker, "rm", "-f", cid], check=False, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="image to scan, e.g. ftf:dev")
    ap.add_argument("--raw", type=Path, help="organiser folder: also flag copies of its files and case names")
    ap.add_argument("--docker", default="docker", help='docker command (default: docker; e.g. "sudo docker")')
    args = ap.parse_args(argv)
    if args.raw is not None and not args.raw.is_dir():
        print(f"image_leak_check: --raw {args.raw} is not a folder", file=sys.stderr)
        return 2
    raw = RawIndex.build(args.raw) if args.raw else None
    with _export(args.image, shlex.split(args.docker)) as stream:
        n, hits = scan_tar(stream, raw)
    for name, rule in hits:
        print(f"LEAK {rule}: /{_norm(name)}")
    print(f"image_leak_check: {args.image}: {n} entries scanned, {len(hits)} hit(s)"
          + (f", raw index {sum(len(v) for v in raw.hashes.values())} files" if raw else ""))
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
