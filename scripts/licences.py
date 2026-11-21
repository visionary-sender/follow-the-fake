"""Generate or check LICENSES-THIRD-PARTY.md from installed package metadata.

Walks the requirements of the installed `ftf` distribution (runtime plus the `dev` extra)
recursively and reads each dependency's licence from its metadata. Run it inside the
project venv (on the VM: `scripts/vm.sh licences`).

    python scripts/licences.py            # print the table to stdout
    python scripts/licences.py --check    # exit 1 if the committed table is stale or a
                                          # licence is not on the permissive allowlist
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "LICENSES-THIRD-PARTY.md"
EXTRAS = ("dev",)

# Matched against whole words of a licence name ("MIT License", "BSD-2-Clause", "PSF-2.0").
ALLOWED_WORDS = {"MIT", "BSD", "0BSD", "APACHE", "PSF", "ISC", "HPND", "ZLIB"}
# Public-domain dedications; checked before the "CC" deny prefix (CC-BY-SA etc. stay denied).
PUBLIC_DOMAIN_WORDS = {"CC0"}
DENIED_PREFIXES = ("GPL", "LGPL", "AGPL", "MPL", "EUPL", "SSPL", "CDDL", "EPL", "CC", "GNU")
DENIED_WORDS = {"MOZILLA", "PROPRIETARY", "COMMERCIAL", "UNLICENSED"}

# Named exceptions to the allowlist, one package at a time. Owner decision (HACK-19, 2026-11-21):
# certifi is the Mozilla CA bundle, MPL-2.0, used unmodified. MPL-2.0 is file-level copyleft and
# puts no obligation on our code. Other MPL packages stay denied. Add a package only if it is an
# unmodified MPL-2.0 data or CA bundle, and record the decision in the PR that adds it.
# Keys are canonical package names; a value must match the metadata licence exactly.
EXCEPTIONS = {
    "certifi": {
        "licence": "Mozilla Public License 2.0 (MPL 2.0)",
        "shown_as": "MPL-2.0 (named exception, unmodified CA bundle; see README Licence section)",
    },
}


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and c.split("::")[-1].strip() != "OSI Approved"
    ]
    if classifiers:
        # Several classifiers usually mean several parts under different licences: all must pass.
        return " AND ".join(sorted(set(classifiers)))
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 80:
        return text
    return "UNKNOWN"


def is_permissive(licence: str) -> bool:
    """At least one OR-alternative must have every AND part permissive."""
    expr = licence.replace("(", " ").replace(")", " ")
    for alt in re.split(r"\s+OR\s+|\s*/\s*|;", expr, flags=re.IGNORECASE):
        alt = re.sub(r"\s+WITH\s+\S+", "", alt.strip(), flags=re.IGNORECASE)  # exceptions only add rights
        parts = re.split(r"\s+AND\s+", alt, flags=re.IGNORECASE)
        if all(_part_permissive(p) for p in parts):
            return True
    return False


def _part_permissive(part: str) -> bool:
    words = re.split(r"[\s,\-]+", part.upper())
    if any(w in PUBLIC_DOMAIN_WORDS for w in words) and not any(w in DENIED_WORDS for w in words):
        return True
    if any(w in DENIED_WORDS or w.startswith(DENIED_PREFIXES) for w in words):
        return False
    return any(w in ALLOWED_WORDS for w in words) or "PYTHON SOFTWARE FOUNDATION" in part.upper()


def is_allowed(name: str, licence: str) -> bool:
    """Permissive licence, or the exact licence of a named exception."""
    exception = EXCEPTIONS.get(name)
    if exception is not None and licence == exception["licence"]:
        return True
    return is_permissive(licence)


def shown_licence(name: str, licence: str) -> str:
    exception = EXCEPTIONS.get(name)
    if exception is not None and licence == exception["licence"]:
        return exception["shown_as"]
    return licence


def dependencies() -> dict[str, metadata.Distribution]:
    found: dict[str, metadata.Distribution] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    queue = [(r, frozenset(EXTRAS)) for r in metadata.requires("ftf") or []]
    while queue:
        raw, extras = queue.pop()
        req = Requirement(raw)
        envs = [{"extra": e} for e in extras] or [{"extra": ""}]
        if req.marker and not any(req.marker.evaluate(env) for env in envs):
            continue
        name = canonicalize_name(req.name)
        key = (name, frozenset(req.extras))
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            raise SystemExit(f"required package not installed: {req.name} (install .[dev])")
        found[name] = dist
        queue.extend((r, frozenset(req.extras)) for r in dist.requires or [])
    return found


def render() -> str:
    lines = [
        "# Third-party licences",
        "",
        "Generated by `scripts/vm.sh licences` from installed package metadata; do not edit.",
        "Covers the runtime dependencies and the `dev` extra of `ftf`, recursively.",
        "",
        "| Package | Licence |",
        "|---|---|",
    ]
    for name, dist in sorted(dependencies().items()):
        lines.append(f"| {name} | {shown_licence(name, licence_of(dist))} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    table = render()
    if not args.check:
        sys.stdout.write(table)
        return 0
    errors = []
    for name, dist in sorted(dependencies().items()):
        if not is_allowed(name, licence_of(dist)):
            errors.append(f"non-permissive or unknown licence: {name} ({licence_of(dist)})")
    if not TABLE.exists() or TABLE.read_text() != table:
        errors.append(f"{TABLE.name} is stale; regenerate with: scripts/vm.sh licences")
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
