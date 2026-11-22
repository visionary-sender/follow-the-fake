"""End to end with the ledger (no stub): the adapter, CLI and contract fit together."""

from __future__ import annotations

from ftf import cli
from ftf.contract.models import Dataset, Decision, Layer, Relation

from linking_support import cap, dataset, incident

L = Layer


def _inc(iid, tenant, owner, landing, pixel):
    caps = [cap(f"{iid}-ad", "ad", arts=("ad_screenshot",)), cap(f"{iid}-l", "lander")]
    vals = {
        f"{iid}-ad": [(L.creative, f"caption-{iid}")],
        f"{iid}-l": [(L.tenant, tenant, True), (L.owner, owner), (L.landing, landing, True), (L.tracking_account, pixel, True), (L.platform, "platform-x")],
    }
    return incident(iid, caps, vals)


def test_link_and_assign_with_the_real_ledger(tmp_path):
    base = [_inc(f"a{i}", "tenant-a", "owner-a", "landing-a", "pixel-a") for i in range(3)]
    base += [_inc(f"b{i}", "tenant-b", "owner-b", "landing-b", "pixel-b") for i in range(3)]
    base += [_inc(f"z{i}", f"tenant-z{i}", f"owner-z{i}", f"landing-z{i}", f"pixel-z{i}") for i in range(4)]
    (tmp_path / "in.json").write_text(dataset(*base).to_json())
    new = dataset(_inc("na", "tenant-a", "owner-a", "landing-a", "pixel-a"), _inc("nn", "tenant-n", "owner-n", "landing-n", "pixel-n"))
    (tmp_path / "new.json").write_text(new.to_json())

    assert cli.main(["link", str(tmp_path / "in.json"), "-o", str(tmp_path / "linked.json")]) == 0
    linked = Dataset.from_json((tmp_path / "linked.json").read_bytes())
    camp = {frozenset(c.capture_ids) for c in linked.campaigns if c.relation is Relation.campaign}
    assert frozenset(f"a{i}-{s}" for i in range(3) for s in ("ad", "l")) in camp
    assert frozenset(f"b{i}-{s}" for i in range(3) for s in ("ad", "l")) in camp

    assert cli.main(["assign", str(tmp_path / "linked.json"), str(tmp_path / "new.json"), "-o", str(tmp_path / "out.json")]) == 0
    out = Dataset.from_json((tmp_path / "out.json").read_bytes())
    decisions = {a.incident_id: a for a in out.assignments}
    assert decisions["na"].decision is Decision.assign
    assert "a0-l" in next(c for c in out.campaigns if c.id == decisions["na"].campaign_id).capture_ids
    assert decisions["nn"].decision is Decision.new
