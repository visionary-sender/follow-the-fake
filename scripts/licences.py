"""Generate or check LICENSES-THIRD-PARTY.md from installed package metadata.

Walks the requirements of the installed `ftf` distribution (runtime plus the `app`, `dev` and `fetch` extras)
recursively and reads each dependency's licence from its metadata. Run it inside the
project venv (on the VM: `scripts/vm.sh licences`).

    python scripts/licences.py            # print the table to stdout
    python scripts/licences.py --check    # exit 1 if the committed table is stale or a
                                          # licence is not on the permissive allowlist

    python scripts/licences.py --audit-image FILE
                                          # FILE: `package<TAB>licence` rows of the system packages
                                          # the image's Chromium step added (see docs/licences.md)

Policy: the permissive allowlist applies to production dependencies, i.e.
what ships in the target container. Named exceptions: certifi (unmodified MPL-2.0 CA bundle) and
the optional `fetch` extra, Playwright plus its browser binaries (Chromium, with its bundled
unmodified LGPL-2.1 FFmpeg, and the Node.js driver). LGPL, GPL and MPL stay denied for everything
else. The Python packages of the `fetch` extra are listed from metadata like any other; the browser
binaries it downloads (`playwright install`) carry no metadata, so `FETCH_BINARIES` lists them.
the Debian system libraries Chromium needs are a named exception too,
see `CHROMIUM_TSV`.
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
EXTRAS = ("app", "dev", "fetch", "features")

# Matched against whole words of a licence name ("MIT License", "BSD-2-Clause", "PSF-2.0").
ALLOWED_WORDS = {"MIT", "BSD", "0BSD", "APACHE", "PSF", "ISC", "HPND", "ZLIB"}
# Public-domain dedications, exempt from the "CC" deny prefix (CC-BY-SA etc. stay denied).
PUBLIC_DOMAIN_WORDS = {"CC0"}
DENIED_PREFIXES = ("GPL", "LGPL", "AGPL", "MPL", "EUPL", "SSPL", "CDDL", "EPL", "CC", "GNU")
DENIED_WORDS = {"MOZILLA", "PROPRIETARY", "COMMERCIAL", "UNLICENSED"}

# Named exceptions to the allowlist, one package at a time.
# certifi is the Mozilla CA bundle, MPL-2.0, used unmodified. MPL-2.0 is file-level copyleft and
# puts no obligation on our code. Other MPL packages stay denied. Add a package only if it is an
# unmodified MPL-2.0 data or CA bundle, and note the exception in the change that adds it.
# Keys are canonical package names; a value must match the metadata licence exactly.
EXCEPTIONS = {
    "certifi": {
        "licence": "Mozilla Public License 2.0 (MPL 2.0)",
        "shown_as": "MPL-2.0 (named exception, unmodified CA bundle; see README Licence section)",
    },
    # tqdm, pulled in by transformers and huggingface_hub, used
    # unmodified; under review. Only this exact licence string is accepted.
    "tqdm": {
        "licence": "MPL-2.0 AND MIT",
        "shown_as": "MPL-2.0 AND MIT (named exception, unmodified)",
    },
    # BSL-1.0 is one part of torch's bundled code, and
    # CNRI-Python is the regex package's own licence. Each is accepted only as torch's or regex's
    # exact licence string, so no other package can use either licence ID.
    "torch": {
        "licence": ("Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause "
                    "AND BSL-1.0 AND ISC AND MIT AND Zlib"),
        "shown_as": "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause "
                    "AND BSL-1.0 AND ISC AND MIT AND Zlib",
    },
    "regex": {
        "licence": "Apache-2.0 AND CNRI-Python",
        "shown_as": "Apache-2.0 AND CNRI-Python",
    },
}

# Named exception: the Debian system libraries that Chromium
# needs, installed in the image by `playwright install --with-deps` (Dockerfile). They are used
# unmodified, and some are LGPL, MPL or GPL in part. Only the packages in CHROMIUM_TSV are accepted,
# each with exactly the licence its copyright file records. Everything else still needs a
# permissive licence. The file is generated from the built image; docs/licences.md has the steps.
CHROMIUM_TSV = ROOT / "scripts" / "chromium-debian-packages.tsv"


def load_chromium_packages(path: Path = CHROMIUM_TSV) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        name, _, licence = line.partition("\t")
        if not licence or name in packages:
            raise SystemExit(f"{path.name}: bad or duplicate row for {name!r}")
        packages[name] = licence
    return packages


# Binaries the `fetch` extra downloads at image build (`playwright install`): no package metadata.
FETCH_PIN = "playwright==1.64.1"  # must equal the `fetch` extra in pyproject.toml
FETCH_BINARIES = [
    ("Playwright driver (Node.js, inside the playwright wheel)", "Apache-2.0; bundled Node.js runtime MIT"),
    ("Chromium headless shell (`playwright install --only-shell chromium`)",
     "BSD-3-Clause; bundles unmodified FFmpeg (LGPL-2.1) and other components under their own licences"),
]


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
    for alt in re.split(r"\s+OR\s+|\s*/\s*", expr, flags=re.IGNORECASE):
        alt = re.sub(r"\s+WITH\s+\S+", "", alt.strip(), flags=re.IGNORECASE)  # exceptions only add rights
        # `;` separates Debian License: fields, and every one of them applies (AND, not OR).
        parts = re.split(r"\s+AND\s+|\s*;\s*", alt, flags=re.IGNORECASE)
        if all(_part_permissive(p) for p in parts):
            return True
    return False


def _part_permissive(part: str) -> bool:
    words = re.split(r"[\s,\-]+", part.upper())
    if any(w in DENIED_WORDS or (w.startswith(DENIED_PREFIXES) and w not in PUBLIC_DOMAIN_WORDS) for w in words):
        return False
    return (any(w in ALLOWED_WORDS or w in PUBLIC_DOMAIN_WORDS for w in words)
            or "PYTHON SOFTWARE FOUNDATION" in part.upper())


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


# Named exception: Debian's ffmpeg, installed by the
# Dockerfile's features slot (FTF_WITH_FFMPEG). Used unmodified and run as a separate program. Its
# copyright file lists GPL parts, so only this exact licence string is accepted, and no other package.
SYSTEM_EXCEPTIONS = {
    "ffmpeg": {
        "licence": ("BSD-1-clause; BSD-2-clause; BSD-3-clause; BSL; Expat; GPL-2+; GPL-3+; IJG; ISC; "
                    "LGPL-2+; LGPL-2.1+; LGPL-2.1+ and BSD-2-clause; LGPL-2.1+ and BSD-3-clause; "
                    "LGPL-2.1+ and BSL; LGPL-2.1+ and Expat; LGPL-2.1+ and Sundry; Sundry; Zlib; "
                    "man-page; public-domain"),
        "shown_as": "ffmpeg (named exception: unmodified, run as a separate program, under review)",
    },
}


def is_allowed_system(name: str, licence: str, chromium: dict[str, str]) -> bool:
    """Permissive licence, the exception (listed in CHROMIUM_TSV with the same licence), or the
    named exception in SYSTEM_EXCEPTIONS (exact licence string)."""
    exception = SYSTEM_EXCEPTIONS.get(name)
    if exception is not None and licence == exception["licence"]:
        return True
    return chromium.get(name) == licence or is_permissive(licence)


def audit_image(rows: dict[str, str], chromium: dict[str, str]) -> list[str]:
    """Compare the system packages of a built image with the list. Returns the errors."""
    errors = [
        f"not permissive and not the exception: {name} ({licence})"
        for name, licence in sorted(rows.items())
        if not is_allowed_system(name, licence, chromium)
    ]
    errors += [f"listed in {CHROMIUM_TSV.name} but absent from the image: {name}"
               for name in sorted(set(chromium) - set(rows))]
    return errors


def features_installed() -> bool:
    """True when every requirement of the `features` extra is installed. The default VM venv has none,
    so `scripts/vm.sh licences` installs the extra and the licence test skips without it."""
    for raw in metadata.requires("ftf") or []:
        req = Requirement(raw)
        in_features = req.marker is not None and req.marker.evaluate({"extra": "features"}) \
            and not req.marker.evaluate({"extra": ""})
        if not in_features:
            continue
        try:
            metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            return False
    return True


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
            raise SystemExit(f"required package not installed: {req.name} (install .[app,dev,fetch,features])")
        found[name] = dist
        queue.extend((r, frozenset(req.extras)) for r in dist.requires or [])
    return found


def render() -> str:
    lines = [
        "# Third-party licences",
        "",
        "Generated by `scripts/vm.sh licences` from installed package metadata; do not edit.",
        "Covers the runtime dependencies and the `app`, `dev` and `fetch` extras of `ftf`, recursively.",
        "",
        "| Package | Licence |",
        "|---|---|",
    ]
    for name, dist in sorted(dependencies().items()):
        lines.append(f"| {name} | {shown_licence(name, licence_of(dist))} |")
    lines += [
        "",
        f"## Browser binaries of the `fetch` extra (`{FETCH_PIN}`)",
        "",
        "Downloaded into the container image by `playwright install` for `ftf fetch` (live page capture).",
        "Named exception, Playwright and its browser binaries are allowed,",
        "including Chromium's bundled unmodified LGPL-2.1 FFmpeg.",
        "",
        "| Component | Licence |",
        "|---|---|",
    ]
    lines += [f"| {name} | {lic} |" for name, lic in FETCH_BINARIES]
    chromium = load_chromium_packages()
    lines += [
        "",
        "## Debian system libraries of Chromium",
        "",
        "Installed in the image by `playwright install --with-deps` (Dockerfile), unmodified, as",
        "Debian packages. Some are LGPL, MPL or GPL in part; they are accepted",
        "as the only exception to the permissive allowlist. Licence per the package's copyright file.",
        "",
        "| Package | Licence |",
        "|---|---|",
    ]
    lines += [f"| {name} | {lic} |" for name, lic in sorted(chromium.items())]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--audit-image", metavar="FILE",
                        help="package<TAB>licence rows of the image's Chromium system packages")
    args = parser.parse_args()
    table = render()
    if args.audit_image:
        rows: dict[str, str] = {}
        for number, line in enumerate(Path(args.audit_image).read_text().splitlines(), 1):
            if not line.strip():
                continue
            name, sep, licence = line.partition("\t")
            if not sep or not licence or name in rows:
                raise SystemExit(f"{args.audit_image}:{number}: expected a unique `package<TAB>licence` row")
            rows[name] = licence
        errors = audit_image(rows, load_chromium_packages())
        for e in errors:
            print(e, file=sys.stderr)
        print(f"audited {len(rows)} image system packages: {len(errors)} errors", file=sys.stderr)
        return 1 if errors else 0
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
