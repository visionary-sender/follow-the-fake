import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "licences.py"
_spec = importlib.util.spec_from_file_location("licences", SCRIPT)
licences = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(licences)


def test_licence_table_is_current_and_permissive():
    out = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    "licence",
    ["MIT", "MIT License", "BSD-2-Clause", "Simplified BSD License", "Apache-2.0 OR BSD-2-Clause",
     "PSF-2.0", "Python Software Foundation License", "Apache-2.0 WITH LLVM-exception",
     "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "CC0-1.0"],
)
def test_permissive(licence):
    assert licences.is_permissive(licence)


def test_certifi_exception_is_exact_and_narrow():
    mpl = licences.EXCEPTIONS["certifi"]["licence"]
    assert licences.is_allowed("certifi", mpl)
    assert not licences.is_allowed("certifi", "GPL-3.0-only")  # the exception is the licence string, not any licence
    assert not licences.is_allowed("some-other-mpl-package", mpl)  # MPL stays denied for everything else
    assert not licences.is_allowed("some-other-mpl-package", "MPL-2.0")
    assert licences.shown_licence("certifi", mpl).startswith("MPL-2.0")
    assert licences.shown_licence("some-other-mpl-package", "MPL-2.0") == "MPL-2.0"


def test_fetch_extra_table_matches_pyproject():
    import tomllib

    data = tomllib.loads((SCRIPT.parent.parent / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert extras["fetch"] == [licences.FETCH_PIN]
    assert not any("playwright" in r or "fetch" in r for r in extras.get("dev", []))  # no browser in dev
    assert not any("playwright" in r for r in data["project"].get("dependencies", []))


def test_lgpl_gpl_mpl_denied_outside_named_exceptions():
    for lic in ("LGPL-2.1-or-later", "GPL-2.0-only", "MPL-2.0", "GNU Lesser General Public License v2 or later (LGPLv2+)"):
        assert not licences.is_allowed("playwright", lic)
        assert not licences.is_allowed("some-package", lic)


@pytest.mark.parametrize(
    "licence",
    ["UNLICENSED", "UNKNOWN", "GPL-3.0-only", "MIT AND CC-BY-NC-4.0", "LGPLv3+",
     "MIT License AND GNU General Public License v3 (GPLv3)", "MPL-2.0", "Permitted use only",
     "CC-BY-SA-4.0", "MIT AND CC-BY-4.0", "CC0 docs, GPL-3.0 code"],
)
def test_not_permissive(licence):
    assert not licences.is_permissive(licence)


def test_chromium_exception_is_exact_and_narrow():
    # HACK-68: only the listed Debian packages, with exactly the recorded licence string.
    chromium = {"libfake-lgpl1": "LGPL-2.1+", "libfake-mit1": "MIT"}
    assert licences.is_allowed_system("libfake-lgpl1", "LGPL-2.1+", chromium)
    assert not licences.is_allowed_system("libfake-lgpl1", "GPL-3+", chromium)
    assert not licences.is_allowed_system("libfake-other", "LGPL-2.1+", chromium)
    assert not licences.is_allowed_system("libfake-other", "GPL-2+", chromium)
    assert licences.is_allowed_system("libfake-other", "MIT", chromium)  # permissive needs no exception


def test_image_audit_reports_unlisted_copyleft_and_missing_entries():
    chromium = {"libfake-lgpl1": "LGPL-2.1+", "libfake-gone": "MIT"}
    rows = {"libfake-lgpl1": "LGPL-2.1+", "libfake-new": "GPL-2+"}
    errors = licences.audit_image(rows, chromium)
    assert errors == [
        "not permissive and not the HACK-68 exception: libfake-new (GPL-2+)",
        "listed in chromium-debian-packages.tsv but absent from the image: libfake-gone",
    ]
    assert licences.audit_image({"libfake-lgpl1": "LGPL-2.1+"}, {"libfake-lgpl1": "LGPL-2.1+"}) == []


def test_committed_chromium_list_loads_and_is_complete():
    chromium = licences.load_chromium_packages()
    assert len(chromium) == 112
    assert all(lic and "UNDETERMINED" not in lic for lic in chromium.values())
