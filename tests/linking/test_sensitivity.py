"""Fragile-link flags (HACK-25, 07 E6): a link within 2 bits of τ is flagged, a strong one is not."""

from __future__ import annotations

import json

from ftf import cli
from ftf.contract.models import Dataset, Decision, Layer, Relation
from ftf.linking import fragile_links, link_dataset, link_incident
from ftf.linking.index import CandidateIndex
from ftf.linking.sensitivity import annotate_assignment, assignment_flips, perturb, summary
from ftf.linking.weights import HAND_TABLE

import synthetic_corpus as sc

L = Layer


def _inc(iid: str, vals: list[tuple[str, str, bool]]):
    b = sc.Builder()
    lander = b.capture(f"{iid}-l", "lander")
    return sc.Incident(id=iid, source="synthetic", captures=[lander], indicators=[b.indicator(lander, k, v, live) for k, v, live in vals])


def _dataset() -> Dataset:
    # n = 6 canonical captures -> campaign tau = log2(6/3) + 3 = 4 bits.
    strong = [("aio_cdn_tenant", "ta.cdn.example", True), ("aio_landing", "la.example", True)]  # 6 + 4 bits
    weak = [("meta_pixel", "pxb", True), ("ad_caption", "caption b", False)]  # 3 + 2 bits
    incs = [
        _inc("a1", strong), _inc("a2", strong),
        _inc("b1", weak), _inc("b2", weak),
        _inc("z1", [("aio_landing", "z1.example", True)]), _inc("z2", [("aio_landing", "z2.example", True)]),
    ]
    return link_dataset(sc.dataset(incs), [Relation.campaign])


def _record(records, a, b):
    return next(r for r in records if {r["a"], r["b"]} == {a, b})


def test_weak_link_is_fragile_and_strong_one_is_not():
    ds = _dataset()
    records = fragile_links(ds)
    weak = _record(records, "b1-l", "b2-l")
    strong = _record(records, "a1-l", "a2-l")
    assert weak["state"] == strong["state"] == "link"
    assert weak["tau"] == 4.0 and weak["bits"] == 5.0
    assert weak["fragile"]
    assert {(f["layer"], f["delta"], f["to"]) for f in weak["flips"]} == {("tracking_account", -2.0, "review"), ("creative", -2.0, "review")}
    assert not strong["fragile"] and strong["flips"] == [] and strong["worst_case"] is None
    s = summary(records)["relations"]["campaign"]
    assert s["links"] == 2 and s["fragile"] == 1


def test_fragile_note_lands_on_the_ledger_rows(tmp_path):
    ds = _dataset()
    (tmp_path / "linked.json").write_text(ds.to_json())
    assert cli.main([
        "sensitivity", str(tmp_path / "linked.json"), "-o", str(tmp_path / "report.json"), "--annotate", str(tmp_path / "out.json"),
    ]) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["relations"]["campaign"]["fragile"] == 1
    out = Dataset.from_json((tmp_path / "out.json").read_bytes())
    weak = next(l for l in out.links if {l.a, l.b} == {"b1-l", "b2-l"})
    notes = {r.layer: r.note or "" for r in weak.evidence}
    assert "fragile: flips to review if tracking_account −2 bits" in notes[L.tracking_account]
    assert "fragile" in notes[L.creative]
    strong = next(l for l in out.links if {l.a, l.b} == {"a1-l", "a2-l"})
    assert not any("fragile" in (r.note or "") for r in strong.evidence)


def test_perturb_moves_one_layer_and_floors_at_zero():
    t = perturb(HAND_TABLE, (L.tracking_account,), -2.0)
    assert t.kinds["meta_pixel"].bits == 1.0
    assert t.kinds["aio_landing"].bits == HAND_TABLE.kinds["aio_landing"].bits
    assert t.layer_defaults[L.tracking_account] == 0.0
    t = perturb(HAND_TABLE, (L.infrastructure,), -2.0)
    assert t.kinds["server_ip"].bits == 0.0
    assert t.kinds["created_date"].bits == 0.0  # zero-weight kinds stay zero under +delta too
    assert perturb(HAND_TABLE, (L.infrastructure,), 2.0).kinds["created_date"].bits == 0.0


def test_assignment_sensitivity_notes_with_and_without_index():
    base = _dataset()
    new = _inc("n1", [("meta_pixel", "pxb", True), ("ad_caption", "caption b", False)])
    res = link_incident(base, new)
    idx = CandidateIndex.build(base, cache=False)
    rec = assignment_flips(base, new, res.assignment, index=idx)
    assert rec == assignment_flips(base, new, res.assignment)
    assert res.assignment.decision is Decision.review  # 5 bits: above tau, below the assign bar
    assert rec["fragile"]
    assert any(f["layer"] == "tracking_account" and f["delta"] == 2.0 and f["to"].startswith("assign") for f in rec["flips"])
    noted = annotate_assignment(res.assignment, rec)
    assert noted.note.startswith(res.assignment.note) and "fragile: flips to assign" in noted.note
    assert "fragile" in noted.candidates[0].note
    strong = _inc("n2", [("aio_cdn_tenant", "ta.cdn.example", True), ("aio_landing", "la.example", True)])
    res2 = link_incident(base, strong)
    assert res2.assignment.decision is Decision.assign
    rec2 = assignment_flips(base, strong, res2.assignment, index=idx)
    assert not rec2["fragile"]
    assert "fragile" not in (annotate_assignment(res2.assignment, rec2).note or "")
