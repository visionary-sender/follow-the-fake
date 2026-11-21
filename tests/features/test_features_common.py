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
    assert v.value is None and v.vector_ref.store.startswith("vectors/org_m_1-") and v.vector_ref.dim == 2
    assert b.provenance.locator == "line=2" and b.provenance.artefact_id == art.id
    w.save()
    doc = read_dataset(write_dataset(ds, out))
    assert len(doc.incidents[0].indicators) == 3
    keys, mat = load_vectors(out, v.vector_ref.store)
    assert keys == [v.id]
    assert np.allclose(mat, [[0.6, 0.8]])

    # another extractor continues the numbering; the same extractor replaces its earlier output
    w2 = IndicatorWriter(ds, out, "other@0")
    assert w2.add(inc, cap, art, Feature(kind="ocr_text", value="!")).id == f"{cap.id}/ocr_text/2"
    w3 = IndicatorWriter(ds, out, "test@1")
    assert [i.extractor for i in inc.indicators] == ["other@0"]
    assert w3.add(inc, cap, art, Feature(kind="ocr_text", value="again")).id == f"{cap.id}/ocr_text/0"
    with pytest.raises(ValueError, match="needs a model"):
        w3.add(inc, cap, art, Feature(kind="x", vector=np.ones(2)))
    with pytest.raises(ValueError, match="exactly one"):
        w3.add(inc, cap, art, Feature(kind="x"))


def test_invalid_indicator_stores_no_vector(raw, tmp_path):
    ds = dataset_from_files([raw / "case 1" / "Zrzut ekranu.png"], raw)
    inc, cap = ds.incidents[0], ds.incidents[0].captures[0]
    w = IndicatorWriter(ds, tmp_path, "t@0")
    with pytest.raises(ValueError):
        # a collector kind outside the collector layer is rejected by the contract
        w.add(inc, cap, None, Feature(kind="viewport", vector=np.ones(2), model="m"))
    assert w.stores["m"].keys == [] and inc.indicators == []
    ok = w.add(inc, cap, None, Feature(kind="viewport_like", vector=np.ones(2), model="m"))
    assert w.stores["m"].keys == [ok.id]


def test_rerun_prunes_orphan_vectors_and_refuses_linked(raw, tmp_path):
    ds = dataset_from_files([raw / "case 1" / "Zrzut ekranu.png"], raw)
    inc, cap = ds.incidents[0], ds.incidents[0].captures[0]
    w = IndicatorWriter(ds, tmp_path, "faces@0")
    refs = [w.add(inc, cap, None, Feature(kind="face_embedding", vector=np.ones(2) * k, model="m")).vector_ref
            for k in (1, 2, 3)]
    w.save()
    w2 = IndicatorWriter(ds, tmp_path, "faces@1")
    w2.add(inc, cap, None, Feature(kind="face_embedding", vector=np.ones(2), model="m"))
    w2.save()
    keys, mat = load_vectors(tmp_path, refs[0].store)
    assert keys == [f"{cap.id}/face_embedding/0"] and mat.shape == (1, 2)

    from ftf.contract.models import EvidenceRow, Link
    ds.links = [Link(id="l", a=cap.id, b="other", relation="campaign", level="loose", method="t",
                     evidence=[EvidenceRow(layer="creative", kind="face_embedding", status="shared",
                                           indicator_ids=[f"{cap.id}/face_embedding/0"])])]
    with pytest.raises(ValueError, match="linking output"):
        IndicatorWriter(ds, tmp_path, "faces@2")


def test_load_vectors_checks_alignment_and_dim(tmp_path):
    s = VectorStore(tmp_path, "m")
    s.add("k", np.ones(2))
    s.save()
    s3 = VectorStore(tmp_path, "m")
    s3.add("k2", np.ones(3))
    with pytest.raises(ValueError, match="stored dim"):
        s3.save()
    (tmp_path / f"{s.store}.keys.json").write_text(json.dumps({"model": "m", "keys": ["a", "b"]}))
    with pytest.raises(ValueError, match="2 keys but 1 rows"):
        load_vectors(tmp_path, s.store)


def test_vector_store_slugs_and_model_check(tmp_path):
    a, b = VectorStore(tmp_path, "a/b"), VectorStore(tmp_path, "a_b")
    assert a.slug != b.slug
    a.add("k", np.ones(2))
    a.save()
    clash = VectorStore(tmp_path, "a/b")
    clash.model = "other"
    clash.add("k2", np.ones(2))
    with pytest.raises(ValueError, match="holds"):
        clash.save()


def test_vector_store_merges_and_replaces(tmp_path):
    s1 = VectorStore(tmp_path, "m")
    s1.add("k1", np.array([1.0, 0.0]))
    s1.add("k2", np.array([0.0, 1.0]))
    s1.save()
    s2 = VectorStore(tmp_path, "m")
    s2.add("k2", np.array([1.0, 1.0]))
    s2.add("k3", np.array([2.0, 0.0]))
    s2.save()
    keys, mat = load_vectors(tmp_path, s2.store)
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
    assert pipeline.apply_all(ds, raw, tmp_path, families=[]) is ds  # empty list runs nothing


def test_pipeline_skips_family_with_missing_dependency(monkeypatch, capsys):
    real = pipeline.importlib.import_module

    def fake(name):
        if name == "ftf.features.image":
            raise ModuleNotFoundError("No module named 'torch'", name="torch")
        return real(name)

    monkeypatch.setattr(pipeline.importlib, "import_module", fake)
    assert "image" not in pipeline.available_families()
    assert "torch" in capsys.readouterr().err


def test_extract_accepts_cross_incident_duplicate(raw, tmp_path, stub_family):
    art = Artefact(id="a", type=ArtefactType.screenshot, path="case 1/Zrzut ekranu.png")
    inc = Incident(
        id="i2",
        source="t",
        captures=[
            Capture(id="c1", stage=Stage.ad, artefacts=[art]),
            Capture(id="c2", stage=Stage.ad, duplicate_of="other-incident-capture"),
        ],
    )
    out = pipeline.extract(inc, raw, tmp_path, families=["text"])
    assert [i.capture_id for i in out.indicators] == ["c1"]


def test_dataset_from_files_rejects_outside_and_dedups(raw, tmp_path):
    f = raw / "case 1" / "Zrzut ekranu.png"
    assert len(dataset_from_files([f, f], raw).incidents) == 1
    outside = tmp_path / "x.png"
    outside.write_bytes(b"x")
    with pytest.raises(SystemExit):
        dataset_from_files([outside], raw)


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
    with pytest.raises(SystemExit):
        main(["features", "--files", str(raw / "case 1" / "Zrzut ekranu.png"), "--raw-root", str(raw),
              "--out", str(out), "--families", " , "])
