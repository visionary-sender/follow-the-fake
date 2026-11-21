import subprocess
import sys

import pytest

from ftf import __version__
from ftf.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ftf {__version__}"


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage: ftf" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: ftf" in capsys.readouterr().out


def test_module_entry_point():
    out = subprocess.run(
        [sys.executable, "-m", "ftf.cli", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"ftf {__version__}"
