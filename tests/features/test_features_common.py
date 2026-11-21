import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from ftf.cli import main
from ftf.contract.models import Artefact, ArtefactType, Capture, Dataset, Incident, Stage
from ftf.features import pipeline
from ftf.features.common import (
    Feature,
    IMAGE_SUFFIXES,
    IMAGE_TYPES,
    IndicatorWriter,
    VectorStore,
    dataset_from_files,
    iter_artefacts,
    load_vectors,
    read_dataset,
    resolve,
    write_dataset,
)


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    (root / "case 1").mkdir(parents=True)
    (root / "case 1" / "Zrzut ekranu.png").write_bytes(b"\x89PNG fake")
    (root / "case 1" / "page.html").write_text("<p>x</p>")
    (root / "case 1" / "clip.mp4").write_bytes(b"\x00")
    return root


def test_dataset_from_files_and_iteration(raw):
    ds = dataset_from_files(sorted(raw.rglob("*.*")), raw)
    assert len(ds.incidents) == 3
    types_ = {inc.captures[0].artefacts[0].type for inc in ds.incidents}
    assert types_ == {ArtefactType.screenshot, ArtefactType.html, ArtefactType.video}
    imgs = list(iter_artefacts(ds, IMAGE_TYPES, IMAGE_SUFFIXES))
    assert [a.path for _, _, a in imgs] == ["case 1/Zrzut ekranu.png"]
    assert resolve(raw, imgs[0][2]) == (raw / "case 1" / "Zrzut ekranu.png").resolve()


def test_iteration_skips_exact_duplicates(raw):
    art = Artefact(id="a", type=ArtefactType.screenshot, path="case 1/Zrzut ekranu.png")
    art2 = Artefact(id="b", type=ArtefactType.screenshot, path="case 1/Zrzut ekranu.png")
    inc = Incident(
        id="i",
        source="t",
        captures=[
            Capture(id="c1", stage=Stage.ad, artefacts=[art]),
            Capture(id="c2", stage=Stage.ad, artefacts=[art2], duplicate_of="c1"),
        ],
    )
    ds = Dataset(name="t", incidents=[inc])
    assert [c.id for _, c, _ in iter_artefacts(ds, IMAGE_TYPES)] == ["c1"]


def test_resolve_refuses_escape(raw):
    art = Artefact(id="a", type=ArtefactType.screenshot, path="../outside.png")
    with pytest.raises(ValueError, match="escapes"):
        resolve(raw, art)
    with pytest.raises(ValueError, match="no path"):
        resolve(raw, Artefact(id="b", type=ArtefactType.url))


def test_writer_ids_vectors_and_roundtrip(raw, tmp_path):
    ds = dataset_from_files([raw / "case 1" / "Zrzut ekranu.png"], raw)
    out = tmp_path / "out"
    w = IndicatorWriter(ds, out, "test@0")
    inc = ds.incidents[0]
    cap = inc.captures[0]
    art = cap.artefacts[0]
    a = w.add(inc, cap, art, Feature(kind="ocr_text", value="Hello"))
    b = w.add(inc, cap, art, Feature(kind="ocr_text", value="World", locator="line=2"))
    v = w.add(inc, cap, art, Feature(kind="text_embedding", vector=np.array([3.0, 4.0]), model="org/m 1"))
    assert [a.id, b.id] == [f"{cap.id}/ocr_text/0", f"{cap.id}/ocr_text/1"]
    assert v.value is None and v.vector_ref.store == "vectors/org_m_1" and v.vector_ref.dim == 2
    assert b.provenance.locator == "line=2" and b.provenance.artefact_id == art.id
    w.save()
    doc = read_dataset(write_dataset(ds, out))
    assert len(doc.incidents[0].indicators) == 3
    keys, mat = load_vectors(out, "vectors/org_m_1")
    assert keys == [v.id]
    assert np.allclose(mat, [[0.6, 0.8]])

    # a second writer on the same dataset continues the numbering
    w2 = IndicatorWriter(ds, out, "test@0")
    assert w2.add(inc, cap, art, Feature(kind="ocr_text", value="!")).id == f"{cap.id}/ocr_text/2"
    with pytest.raises(ValueError, match="needs a model"):
        w2.add(inc, cap, art, Feature(kind="x", vector=np.ones(2)))


def test_vector_store_merges_and_replaces(tmp_path):
    s1 = VectorStore(tmp_path, "m")
    s1.add("k1", np.array([1.0, 0.0]))
    s1.add("k2", np.array([0.0, 1.0]))
    s1.save()
    s2 = VectorStore(tmp_path, "m")
    s2.add("k2", np.array([1.0, 1.0]))
    s2.add("k3", np.array([2.0, 0.0]))
    s2.save()
    keys, mat = load_vectors(tmp_path, "vectors/m")
    assert keys == ["k1", "k2", "k3"]
    assert np.allclose(mat[1], [2**-0.5, 2**-0.5])
    with pytest.raises(ValueError, match="dim"):
        s2.add("k4", np.ones(3))


@pytest.fixture
def stub_family(monkeypatch):
    mod = types.ModuleType("ftf.features.text")

    def apply(ds, raw_root, out_dir):
        w = IndicatorWriter(ds, out_dir, "stub@0")
        for inc, cap, art in iter_artefacts(ds, IMAGE_TYPES, IMAGE_SUFFIXES):
            w.add(inc, cap, art, Feature(kind="stub", value=Path(art.path).name))
        w.save()
        return ds

    mod.apply = apply
    monkeypatch.setitem(sys.modules, "ftf.features.text", mod)
    return mod


def test_pipeline_with_stub_family(raw, tmp_path, stub_family):
    assert "text" in pipeline.available_families()
    ds = dataset_from_files([raw / "case 1" / "Zrzut ekranu.png"], raw)
    inc = pipeline.extract(ds.incidents[0], raw, tmp_path / "o", families=["text"])
    assert [i.value for i in inc.indicators] == ["Zrzut ekranu.png"]
    with pytest.raises(ValueError, match="unknown"):
        pipeline.apply_all(ds, raw, tmp_path, families=["nope"])


def test_cli_features(raw, tmp_path, stub_family, capsys):
    out = tmp_path / "cli"
    rc = main(
        [
            "features",
            "--files",
            str(raw / "case 1" / "Zrzut ekranu.png"),
            "--raw-root",
            str(raw),
            "--out",
            str(out),
            "--families",
            "text",
        ]
    )
    assert rc == 0
    doc = json.loads((out / "dataset.json").read_text())
    assert doc["incidents"][0]["indicators"][0]["kind"] == "stub"
    with pytest.raises(SystemExit):
        main(["features", "--out", str(out)])
