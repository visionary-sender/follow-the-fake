import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "licences.py"


def test_licence_table_is_current_and_permissive():
    out = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
