"""scripts/egress.sh dry run: every worker is created with the safety flags (no cloud call)."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "egress.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_create_flags():
    out = subprocess.run(["bash", str(SCRIPT), "flags"], capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin", "FTF_GCP_PROJECT": "test-project", "FTF_BUCKET": "test-bucket"}).stdout.split()
    joined = " ".join(out)
    assert out[:4] == ["gcloud", "compute", "instances", "create"]
    assert "--labels ftf-fleet=egress,purpose=egress" in joined
    assert "--max-run-duration 2h --instance-termination-action DELETE" in joined
    assert "--no-service-account --no-scopes" in joined
    assert "auto-delete=yes" in joined and "--project test-project" in joined
    assert "--address" not in joined  # ephemeral IP only
