"""A tiny synthetic campaign base (no organiser data), shared by the pipeline and app tests."""

from __future__ import annotations

from pathlib import Path

from ftf.contract import (
    Artefact,
    ArtefactType,
    Assignment,
    Campaign,
    Candidate,
    Capture,
    Dataset,
    Decision,
    EvidenceRow,
    EvidenceStatus,
    Incident,
    Indicator,
    Layer,
    Level,
    Link,
    LinkLevel,
    MissingModality,
    Observed,
    Relation,
    Stage,
)


def _incident(n: int, pixel: str) -> Incident:
    iid = f"s-{n}"
    ad = Capture(
        id=f"{iid}/ad",
        stage=Stage.ad,
        artefacts=[Artefact(id=f"{iid}/ad/a1", type=ArtefactType.ad_screenshot)],
        missing=[MissingModality(modality=ArtefactType.video, reason="not submitted")],
    )
    lander = Capture(
        id=f"{iid}/lander",
        stage=Stage.lander,
        artefacts=[Artefact(id=f"{iid}/lander/a1", type=ArtefactType.har), Artefact(id=f"{iid}/lander/a2", type=ArtefactType.html)],
    )
    ind = [
        Indicator(id=f"{iid}/pixel", capture_id=lander.id, layer=Layer.tracking_account, kind="meta_pixel", level=Level.operator,
                  value=pixel, observed=Observed.live, extractor="synthetic@0"),
        Indicator(id=f"{iid}/domain", capture_id=lander.id, layer=Layer.landing, kind="landing_domain", level=Level.landing,
                  value=f"https://shop{n}.example.com/offer", observed=Observed.static, extractor="synthetic@0"),
        Indicator(id=f"{iid}/vp", capture_id=lander.id, layer=Layer.collector, kind="viewport", level=Level.capture,
                  value="1920x1080", observed=Observed.static, extractor="synthetic@0"),
    ]
    return Incident(id=iid, source="synthetic", captures=[ad, lander], indicators=ind)


def synthetic_base() -> Dataset:
    incidents = [_incident(1, "111"), _incident(2, "111"), _incident(3, "222")]
    link = Link(
        id="L1", a="s-1/lander", b="s-2/lander", relation=Relation.campaign, level=LinkLevel.strict, score_bits=12.0,
        method="synthetic@0",
        evidence=[
            EvidenceRow(layer=Layer.tracking_account, kind="meta_pixel", status=EvidenceStatus.shared, bits=12.0,
                        indicator_ids=["s-1/pixel", "s-2/pixel"]),
            EvidenceRow(layer=Layer.collector, kind="viewport", status=EvidenceStatus.collector_excluded,
                        indicator_ids=["s-1/vp", "s-2/vp"]),
            EvidenceRow(layer=Layer.tracking_account, kind="google_ads_id", status=EvidenceStatus.missing),
        ],
    )
    weak = Link(
        id="L2", a="s-2/lander", b="s-3/lander", relation=Relation.ecosystem, level=LinkLevel.loose, score_bits=2.0,
        method="synthetic@0",
        evidence=[EvidenceRow(layer=Layer.landing, kind="landing_domain", status=EvidenceStatus.conflict, bits=-1.0,
                              indicator_ids=["s-2/domain", "s-3/domain"])],
    )
    campaigns = [
        Campaign(id="C1", label="synthetic pixel campaign", relation=Relation.campaign,
                 capture_ids=["s-1/ad", "s-1/lander", "s-2/ad", "s-2/lander"], link_ids=["L1"]),
        Campaign(id="C2", label="synthetic singleton", relation=Relation.campaign, capture_ids=["s-3/lander"]),
    ]
    assignment = Assignment(
        incident_id="s-2", candidates=[Candidate(campaign_id="C1", score_bits=12.0), Candidate(campaign_id="C2", score_bits=0.0)],
        decision=Decision.assign, campaign_id="C1", evidence_capacity_bits=20.0, method="synthetic@0",
    )
    return Dataset(name="synthetic base", incidents=incidents, links=[link, weak], campaigns=campaigns, assignments=[assignment])


def write_base(tmp_path: Path) -> Path:
    path = tmp_path / "base.json"
    path.write_text(synthetic_base().to_json(), encoding="utf-8")
    return path
