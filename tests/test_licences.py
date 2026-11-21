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
     "PSF-2.0", "Python Software Foundation License", "Apache-2.0 WITH LLVM-exception"],
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


@pytest.mark.parametrize(
    "licence",
    ["UNLICENSED", "UNKNOWN", "GPL-3.0-only", "MIT AND CC-BY-NC-4.0", "LGPLv3+",
     "MIT License AND GNU General Public License v3 (GPLv3)", "MPL-2.0", "Permitted use only"],
)
def test_not_permissive(licence):
    assert not licences.is_permissive(licence)
