from ftf.app import ledger
from ftf.contract import Assignment, Decision, MissingModality
from ftf.pipeline import AnalysisInput, analyze
from pipeline_synthetic import synthetic_base


def test_defang_and_display():
    assert ledger.defang("https://a.example.com/x") == "hxxps://a[.]example[.]com/x"
    assert ledger.display_value("shop.example.pl") == "shop[.]example[.]pl"
    assert ledger.display_value("1920x1080") == "1920x1080"
    long = ledger.display_value("x" * 200)
    assert len(long) == ledger.MAX_VALUE_CHARS and long.endswith("…")
    assert ledger.display_value("a\nb") == "a b"


def test_ledger_rows_statuses_and_seen_in(tmp_path):
    base = synthetic_base()
    res = analyze(AnalysisInput(kind="dataset_item", text="s-2"), base, run_dir=tmp_path)
    ds = res.to_dataset()
    link = next(l for l in res.links if l.id == "L1")
    rows = ledger.ledger_rows(link, ds, population=base)
    by_kind = {r.kind: r for r in rows}
    assert by_kind["meta_pixel"].status == "shared" and by_kind["meta_pixel"].seen_in == "2 / 3"
    assert by_kind["meta_pixel"].bits == 12.0
    assert by_kind["viewport"].greyed and "collector" in by_kind["viewport"].note
    assert "not a penalty" in by_kind["google_ads_id"].note
    assert rows[-1].greyed  # collector rows last
    conflict = ledger.ledger_rows(next(l for l in res.links if l.id == "L2"), ds, population=base)[0]
    assert conflict.status == "conflict" and "[.]" in conflict.value and "http" not in conflict.value


def test_relation_sentence_and_tier():
    base = synthetic_base()
    strong, weak = base.links
    t = ledger.review_threshold_bits(base)
    assert ledger.relation_sentence(strong, t) == "same campaign: yes, +12.0 bits (Strong; 1 shared layer(s))"
    assert ledger.tier(weak, t) == ledger.INSUFFICIENT
    assert ledger.relation_sentence(weak, t).startswith("same ecosystem: insufficient alone, +2.0 bits")


def test_what_would_change():
    a = Assignment(incident_id="x", candidates=[], decision=Decision.review, evidence_capacity_bits=0.0, method="m")
    lines = ledger.what_would_change(a, [MissingModality(modality="har", reason="not submitted")])
    assert lines[0].startswith("No candidate campaign")
    assert lines[1] == "HAR missing: up to +6 bits available from the tracking and infrastructure layers (not submitted)."


def test_decision_summary_features_similar_stages(tmp_path):
    base = synthetic_base()
    res = analyze(AnalysisInput(kind="dataset_item", text="s-2"), base, run_dir=tmp_path)
    card = ledger.decision_summary(res.assignment, base)
    assert card["label"] == "Assign to campaign" and card["campaign_label"] == "synthetic pixel campaign"
    assert [c["campaign"] for c in card["candidates"]] == ["C1", "C2"]
    groups = ledger.feature_rows(res.incident)
    assert list(groups) == ["tracking_account", "landing", "collector"]
    assert groups["landing"][0]["value"].startswith("hxxps://")
    sim = ledger.similar_rows(res.similar, base)
    assert sim[0]["tier"] == "Strong" and sim[1]["tier"] == ledger.INSUFFICIENT
    assert len(ledger.stage_rows(res.stages)) == 4
    assert [m.modality.value for m in ledger.missing_modalities(res.incident)] == ["video"]
