import importlib.util
import struct
from pathlib import Path

import pytest

from ftf.contract.fixture import fixture_dir

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_campaign_graph.py"
_spec = importlib.util.spec_from_file_location("make_campaign_graph", SCRIPT)
mcg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcg)


def _png_width(path: Path) -> int:
    with path.open("rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">I", head[16:20])[0]


def test_campaign_graph_png_is_1200_px_wide(tmp_path):
    dataset = fixture_dir() / "dataset.json"
    if not dataset.exists():
        pytest.skip("fixture absent (see docs/contract.md)")
    from ftf.contract import Dataset

    ds = Dataset.from_json(dataset.read_bytes())
    inc = mcg.incident_of(ds)
    multi = [c for c in ds.campaigns if len({inc[x] for x in c.capture_ids}) >= 2]
    if not multi:
        pytest.skip("fixture has no campaign with two incidents")
    out = tmp_path / "campaign_graph.png"
    mcg.main([str(dataset), str(out), "--campaign", multi[0].id])
    assert out.exists()
    assert _png_width(out) == mcg.WIDTH_PX
