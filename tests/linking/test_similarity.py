"""HACK-24: calibrated continuous similarity. Synthetic data only, built in the tests."""

import json
import math

import numpy as np
import pytest

from ftf.contract.models import Dataset, EvidenceStatus, Indicator, VectorRef
from ftf.features.common import VectorStore as VectorWriter
from ftf.linking.similarity import (
    MIN_POSITIVES,
    VIEWS,
    Calibration,
    DatasetVectorStore,
    View,
    cllr,
    cllr_min,
    cosine,
    hamming_similarity,
    jaccard_similarity,
    load_calibrations,
    minhash_similarity,
    register_view,
    save_calibrations,
    view_evidence,
    view_for_kind,
)

MODEL = "test/model-a"
STORE = VectorWriter(".", MODEL).store  # vectors/<safe-model>-<sha8>


def rng(seed=0):
    return np.random.default_rng(seed)


def null_sims(n=2000, seed=0):
    """Cosine-like null: unrelated pairs cluster around 0.75 (same genre, different campaigns)."""
    return np.clip(rng(seed).normal(0.75, 0.04, n), -1, 1)


def pos_sims(n, seed=1):
    return np.clip(rng(seed).normal(0.93, 0.03, n), -1, 1)


def vec_ind(cap, n=0, kind="text_embedding", model=MODEL, dim=8, store=STORE):
    iid = f"{cap}/{kind}/{n}"
    return Indicator(id=iid, capture_id=cap, layer="creative", kind=kind, level="campaign",
                     vector_ref=VectorRef(store=store, key=iid, model=model, dim=dim),
                     observed="static", extractor="test@0")


def val_ind(cap, value, kind="image_phash", n=0):
    return Indicator(id=f"{cap}/{kind}/{n}", capture_id=cap, layer="creative", kind=kind,
                     level="campaign", value=value, observed="static", extractor="test@0")


def write_store(base, vectors: dict, model=MODEL):
    """Write vectors with Visual's own writer, so the layout is exactly the features stage's."""
    w = VectorWriter(base, model)
    for k, v in vectors.items():
        w.add(k, np.asarray(v, dtype=np.float32))
    w.save()
    return DatasetVectorStore(base)


# --------------------------------------------------------------------------- similarities


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine([0, 0], [1, 0]) is None
    assert cosine([1, 0], [1, 0, 0]) is None
    assert cosine([np.nan, 1], [1, 0]) is None


def test_hamming_similarity():
    assert hamming_similarity("ffff0000ffff0000", "ffff0000ffff0000") == 1.0
    assert hamming_similarity("0000000000000000", "ffffffffffffffff") == 0.0
    assert hamming_similarity("0000000000000000", "000000000000000f") == pytest.approx(1 - 4 / 64)
    assert hamming_similarity("0x00ff", "00FF") == 1.0
    assert hamming_similarity("00ff", "00ff00ff") is None  # different bit lengths
    assert hamming_similarity("zz", "00") is None


def test_minhash_and_jaccard():
    assert minhash_similarity("1,2,3,4", "1,2,9,9") == 0.5
    assert minhash_similarity("1 2 3", "1 2") is None
    assert jaccard_similarity("a b c", "b c d") == pytest.approx(2 / 4)
    assert jaccard_similarity("", "a") is None


def test_view_registry_defaults_and_extension():
    for kind in ("text_embedding", "image_embedding", "face_embedding", "image_phash", "image_dhash", "image_pdq"):
        assert view_for_kind(kind) is not None
    assert view_for_kind("domain") is None
    v = register_view(View("test_hash_view", "value", "hamming", 0.0, 1.0, hamming_similarity))
    try:
        assert view_for_kind("test_hash_view") is v
    finally:
        VIEWS.pop("test_hash_view")
    with pytest.raises(ValueError):
        register_view(View("bad_view", "value", "x", 0.0, 1.0))


# --------------------------------------------------------------------------- calibration


@pytest.mark.parametrize("n_pos", [0, 3, MIN_POSITIVES - 1, MIN_POSITIVES, 40])
def test_monotone_capped_non_negative(n_pos):
    cal = Calibration.fit(null_sims(), pos_sims(n_pos) if n_pos else None, view="text_embedding")
    grid = np.linspace(-1, 1, 4001)
    b = cal.bits_array(grid)
    assert np.all(b >= 0)
    assert np.all(b <= cal.cap_bits + 1e-12)
    assert np.all(np.diff(b) >= -1e-12)


def test_method_switches_at_positive_threshold():
    assert Calibration.fit(null_sims()).method == "tail_lr"
    assert Calibration.fit(null_sims(), pos_sims(MIN_POSITIVES - 1)).method == "tail_lr"
    cal = Calibration.fit(null_sims(), pos_sims(MIN_POSITIVES))
    assert cal.method == "logistic"
    assert cal.slope > 0


def test_tail_lr_unseen_high_similarity_near_cap_typical_null_zero():
    null = null_sims(20000)
    cal = Calibration.fit(null, view="text_embedding")
    assert null.max() < 0.95
    assert cal.bits(0.99) >= cal.cap_bits - 0.1  # never reached by the null -> near cap
    assert cal.bits(float(np.median(null))) == 0.0  # typical unrelated pair -> nothing
    assert cal.bits(float(np.quantile(null, 0.75))) == 0.0
    assert cal.bits(-0.5) == 0.0


def test_tail_lr_max_bounded_by_null_size():
    small = Calibration.fit(null_sims(50), cap_bits=100.0)
    # p-to-e calibrator with kappa: max bits = log2(kappa) + (1 - kappa) * log2(n + 1)
    expected = math.log2(small.kappa) + (1 - small.kappa) * math.log2(51)
    assert small.bits(1.0) == pytest.approx(expected)
    assert small.bits(1.0) <= math.log2(51)


def test_logistic_separates_and_is_bounded_by_tail():
    null, pos = null_sims(5000), pos_sims(30)
    cal = Calibration.fit(null, pos, view="text_embedding")
    assert cal.bits(0.75) == 0.0
    assert cal.bits(0.97) > 2.0
    # never more than the tail bound log2((n+1)/(k+1))
    for s in (0.8, 0.85, 0.9, 0.95):
        k = int(np.sum(null >= s))
        assert cal.bits(s) <= math.log2((len(null) + 1) / (k + 1)) + 1e-9


def test_logistic_uninformative_view_gives_zero():
    null = null_sims(3000, seed=4)
    pos = null_sims(30, seed=5) - 0.05  # positives *less* similar than the null
    cal = Calibration.fit(null, pos)
    assert cal.method == "logistic"
    assert cal.slope <= 0
    assert cal.bits(1.0) == 0.0
    assert "warning" in cal.quality


def test_missing_and_nan_give_zero():
    cal = Calibration.fit(null_sims())
    assert cal.bits(None) == 0.0
    assert cal.bits(float("nan")) == 0.0


def test_quality_report_is_json_and_complete():
    cal = Calibration.fit(null_sims(), pos_sims(25), view="text_embedding")
    q = json.loads(json.dumps(cal.quality))
    for key in ("method", "n_null", "n_pos", "cap_bits", "auc", "cllr", "cllr_min", "sim_at_bits", "max_bits"):
        assert key in q
    assert q["auc"] > 0.95
    assert q["cllr"] < 1.0
    assert 0.0 <= q["cllr_min"] <= q["cllr"] + 1e-6
    s1, s2, s4 = (q["sim_at_bits"][k] for k in ("1", "2", "4"))
    assert s1 is not None and s1 <= s2 <= s4
    assert cal.bits(s4) >= 4.0 - 1e-9
    tail_q = Calibration.fit(null_sims()).quality
    assert tail_q["method"] == "tail_lr" and "auc" not in tail_q


def test_cllr_reference_values():
    assert cllr(np.zeros(5), np.zeros(5)) == pytest.approx(1.0)
    assert cllr(np.full(5, 20.0), np.full(5, -20.0)) == pytest.approx(0.0, abs=1e-6)
    assert cllr_min(np.array([0.9, 0.95]), np.array([0.1, 0.2])) == pytest.approx(0.0, abs=1e-9)
    assert cllr_min(np.array([0.5, 0.5]), np.array([0.5, 0.5])) == pytest.approx(1.0)


def test_json_round_trip(tmp_path):
    for pos in (None, pos_sims(20)):
        cal = Calibration.fit(null_sims(), pos, view="image_embedding")
        back = Calibration.from_json(cal.to_json())
        grid = np.linspace(-1, 1, 801)
        assert np.array_equal(back.bits_array(grid), cal.bits_array(grid))
        assert back.quality == json.loads(json.dumps(cal.quality))
        cal.save(tmp_path / "c.json")
        assert Calibration.load(tmp_path / "c.json").method == cal.method
    cals = {"text_embedding": Calibration.fit(null_sims()), "image_phash": Calibration.fit(rng(3).uniform(0.3, 0.7, 500))}
    save_calibrations(cals, tmp_path / "all.json")
    back = load_calibrations(tmp_path / "all.json")
    assert set(back) == set(cals)
    assert back["image_phash"].bits(0.99) == cals["image_phash"].bits(0.99)
    with pytest.raises(ValueError):
        Calibration.from_dict({"format": "other"})


def test_fit_rejects_bad_input():
    with pytest.raises(ValueError):
        Calibration.fit([])
    with pytest.raises(ValueError):
        Calibration.fit([0.1, 0.2], cap_bits=0)


# --------------------------------------------------------------------------- vector store


def test_dataset_vector_store(tmp_path):
    a, b = vec_ind("c1"), vec_ind("c2")
    store = write_store(tmp_path, {a.id: np.arange(8) + 1.0})
    v = store.get(a.vector_ref, a.capture_id)
    assert v is not None and v.shape == (8,)
    assert np.linalg.norm(v) == pytest.approx(1.0, rel=1e-5)
    assert store.get(b.vector_ref, b.capture_id) is None  # key absent
    assert store.get(vec_ind("c1", store="vectors/nope").vector_ref) is None  # store absent
    assert store.get(vec_ind("c1", dim=16).vector_ref) is None  # wrong dim
    assert store.get(vec_ind("c1", store="../outside").vector_ref) is None  # leaves base dir
    assert DatasetVectorStore().get(a.vector_ref, a.capture_id) is None  # no directory known


def test_dataset_vector_store_malformed(tmp_path):
    d = tmp_path / "vectors"
    d.mkdir()
    np.save(d / (STORE.split("/")[1] + ".npy"), np.zeros((2, 8), np.float32))
    (d / (STORE.split("/")[1] + ".keys.json")).write_text(json.dumps({"model": MODEL, "keys": ["only-one"]}))
    assert DatasetVectorStore(tmp_path).get(vec_ind("c1").vector_ref) is None


def _dataset(name, inc_id, cap_id, indicators):
    return Dataset.model_validate({
        "name": name,
        "incidents": [{"id": inc_id, "source": "test", "captures": [{"id": cap_id, "stage": "lander"}],
                       "indicators": [i.model_dump(mode="json") for i in indicators]}],
    })


def test_vectors_resolve_per_dataset_directory(tmp_path):
    """A new incident assigned against a collection: each side's vectors live in its own out_dir,
    under the same store name and possibly the same key."""
    base_dir, new_dir = tmp_path / "collection", tmp_path / "new"
    old, new = vec_ind("old1"), vec_ind("new1")
    u = np.eye(8)[0]
    write_store(base_dir, {old.id: u, "shadow": -u})
    write_store(new_dir, {new.id: 0.999 * u + 0.0447 * np.eye(8)[1], "shadow": u})
    coll = _dataset("collection", "inc-old", "old1", [old])
    fresh = _dataset("new", "inc-new", "new1", [new])
    store = DatasetVectorStore.for_datasets((coll, base_dir), (fresh, new_dir))
    assert store.get(old.vector_ref, "old1") is not None
    assert store.get(new.vector_ref, "new1") is not None
    assert store.get(new.vector_ref, "old1") is None  # the new key is not in the collection's store
    cal = Calibration.fit(null_sims(20000), view="text_embedding")
    row = view_evidence(VIEWS["text_embedding"], old, new, store, cal)
    assert row.status is EvidenceStatus.shared and row.bits > 3.5
    # same key in both directories resolves to the right file
    shadow_old = vec_ind("old1", n=9).model_copy(update={"vector_ref": old.vector_ref.model_copy(update={"key": "shadow"})})
    shadow_new = vec_ind("new1", n=9).model_copy(update={"vector_ref": old.vector_ref.model_copy(update={"key": "shadow"})})
    assert store.get(shadow_old.vector_ref, "old1") @ store.get(shadow_new.vector_ref, "new1") == pytest.approx(-1.0, abs=1e-5)
    # incremental: add_dataset on an existing store
    s2 = DatasetVectorStore(base_dir)
    assert s2.get(new.vector_ref, "new1") is None
    s2.add_dataset(fresh, new_dir)
    assert s2.get(new.vector_ref, "new1") is not None


# --------------------------------------------------------------------------- evidence rows


def _embedding_case(tmp_path, sim_target):
    """Two captures whose 8-d vectors have roughly the requested cosine."""
    a, b = vec_ind("c1"), vec_ind("c2")
    u = np.zeros(8); u[0] = 1
    w = np.zeros(8); w[1] = 1
    store = write_store(tmp_path, {a.id: u, b.id: sim_target * u + math.sqrt(1 - sim_target ** 2) * w})
    return a, b, store


def test_view_evidence_shared_for_high_similarity(tmp_path):
    a, b, store = _embedding_case(tmp_path, 0.99)
    cal = Calibration.fit(null_sims(20000), view="text_embedding")
    row = view_evidence(VIEWS["text_embedding"], a, b, store, cal)
    assert row is not None
    assert row.status is EvidenceStatus.shared
    assert row.layer.value == "creative" and row.kind == "text_embedding"
    assert 0 < row.bits <= cal.cap_bits
    assert row.indicator_ids == [a.id, b.id]
    assert "0.990" in row.note and "tail_lr" in row.note


def test_view_evidence_none_for_typical_null(tmp_path):
    a, b, store = _embedding_case(tmp_path, 0.75)
    cal = Calibration.fit(null_sims(20000))
    assert view_evidence(VIEWS["text_embedding"], a, b, store, cal) is None


def test_view_evidence_missing(tmp_path):
    a, b, store = _embedding_case(tmp_path, 0.99)
    cal = Calibration.fit(null_sims())
    view = VIEWS["text_embedding"]
    row = view_evidence(view, a, [], store, cal)
    assert row.status is EvidenceStatus.missing and row.bits == 0.0
    absent = vec_ind("c3")  # referenced but not in the store
    row = view_evidence(view, a, absent, store, cal)
    assert row.status is EvidenceStatus.missing and row.bits == 0.0
    other = vec_ind("c2", model="other/model")
    row = view_evidence(view, a, other, store, cal)
    assert row.status is EvidenceStatus.missing
    assert view_evidence(view, [], None, store, cal) is None
    # wrong kind on one side counts as absent
    row = view_evidence(view, a, vec_ind("c2", kind="image_embedding"), store, cal)
    assert row.status is EvidenceStatus.missing


def test_view_evidence_phash_best_pair():
    base = "f0f0f0f0f0f0f0f0"
    far = "0f0f0f0f0f0f0f0f"
    near = "f0f0f0f0f0f0f0f1"  # 1 bit off
    a = [val_ind("c1", far, n=0), val_ind("c1", base, n=1)]
    b = val_ind("c2", f"00000000ffffffff,{near}")  # multi-hash value: the second one matches
    null = rng(7).binomial(64, 0.5, 5000)
    cal = Calibration.fit(1 - null / 64, view="image_phash")
    row = view_evidence(VIEWS["image_phash"], a, b, None, cal)
    assert row.status is EvidenceStatus.shared
    assert row.indicator_ids == ["c1/image_phash/1", "c2/image_phash/0"]
    assert row.bits == pytest.approx(cal.cap_bits, abs=0.1)
    assert "0.984" in row.value


def test_view_evidence_pdq_256_bit():
    rnd = rng(11)
    h = "".join(rnd.choice(list("0123456789abcdef"), 64))
    null = rnd.binomial(256, 0.5, 3000)
    cal = Calibration.fit(1 - null / 256, view="image_pdq")
    row = view_evidence(VIEWS["image_pdq"], val_ind("c1", h, kind="image_pdq"), val_ind("c2", h, kind="image_pdq"), None, cal)
    assert row.status is EvidenceStatus.shared and row.bits > 3.5
