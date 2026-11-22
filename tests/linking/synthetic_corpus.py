"""A random synthetic corpus with the hand-table kinds (invented `.example` values only, no real data).

Operators run a tenant, an owner, pixels, landers and kits; incidents are ads plus landers with
dropouts, decoys, static vs live observations, exact duplicates and noise. Used to compare the
indexed assignment with the linear scan, and for the scale-up timings (`replicate`).
"""

from __future__ import annotations

import random
from typing import Optional

from ftf.contract.models import (
    Artefact,
    ArtefactType,
    Capture,
    Dataset,
    Incident,
    Indicator,
    Layer,
    Level,
    Observed,
    Provenance,
    Stage,
)

L = Layer
KINDS = {
    "aio_cdn_tenant": (L.tenant, ArtefactType.har),
    "aio_owner": (L.owner, ArtefactType.har),
    "aio_campaign": (L.owner, ArtefactType.har),
    "aio_landing": (L.landing, ArtefactType.html),
    "meta_pixel": (L.tracking_account, ArtefactType.har),
    "kit_asset_set": (L.kit, ArtefactType.html),
    "platform_name": (L.platform, ArtefactType.html),
    "ad_caption": (L.creative, ArtefactType.ad_screenshot),
    "server_ip": (L.infrastructure, ArtefactType.har),
    "analyst_ip": (L.collector, ArtefactType.har),
}
# Indicator.level per kind for `corpus(chain=True)` (else every indicator is campaign level).
LEVELS = {
    "aio_cdn_tenant": Level.tenant, "aio_owner": Level.operator, "aio_campaign": Level.campaign,
    "aio_landing": Level.landing, "meta_pixel": Level.operator, "kit_asset_set": Level.platform,
    "platform_name": Level.platform,
}


class Builder:
    def __init__(self, levels: bool = False) -> None:
        self.n = 0
        self.levels = levels

    def capture(self, cid: str, stage: str, duplicate_of: Optional[str] = None) -> Capture:
        arts = ("ad_screenshot",) if stage == "ad" else ("html", "har")
        return Capture(
            id=cid,
            stage=Stage(stage),
            artefacts=[Artefact(id=f"{cid}/{t}", type=ArtefactType(t)) for t in arts],
            duplicate_of=duplicate_of,
        )

    def indicator(self, cap: Capture, kind: str, value: str, live: bool = False) -> Indicator:
        layer, art = KINDS[kind]
        self.n += 1
        has = any(a.type is art for a in cap.artefacts)
        return Indicator(
            id=f"{cap.id}:i{self.n}",
            capture_id=cap.id,
            layer=layer,
            kind=kind,
            level=LEVELS.get(kind, Level.campaign) if self.levels else Level.campaign,
            value=value,
            observed=Observed.live if live else Observed.static,
            provenance=Provenance(artefact_id=f"{cap.id}/{art.value}" if has else None),
            extractor="synthetic@0",
        )


def corpus(n_incidents: int = 60, n_operators: int = 8, seed: int = 7, prefix: str = "", chain: bool = False) -> list[Incident]:
    """`n_incidents` random incidents over `n_operators` operators (about a fifth are noise).

    `chain=True` adds the aio campaign id (two campaigns per owner, set by the landing) and gives each
    kind its usual Indicator.level, so the chain rule has a full tenant ⊃ owner ⊃ campaign ⊃ landing
    chain to work on. The random draws are the same either way.
    """
    rnd = random.Random(seed)
    b = Builder(levels=chain)
    ops = []
    for o in range(n_operators):
        ops.append({
            "tenant": f"{prefix}t{o}.cdn.example",
            "owner": f"{prefix}owner-{o}",
            "pixels": [f"{prefix}px{o}{j}" for j in range(2)],
            "landings": [f"{prefix}l{o}{j}.example" for j in range(3)],
            "kit": f"{prefix}kit-{o % max(n_operators // 2, 1)}",  # kits are shared by two operators
            "captions": [f"{prefix}caption {o} {j}" for j in range(3)],
            "ip": f"198.51.100.{(o * 7) % 250}",
        })
    out: list[Incident] = []
    all_caps: list[Capture] = []
    for i in range(n_incidents):
        iid = f"{prefix}inc{i:04d}"
        noise = rnd.random() < 0.2
        op = ops[rnd.randrange(n_operators)]
        caps, inds = [], []
        ad = b.capture(f"{iid}-ad", "ad")
        caps.append(ad)
        caption = f"{prefix}noise caption {i}" if noise else rnd.choice(op["captions"])
        inds.append(b.indicator(ad, "ad_caption", caption))
        stage = "decoy" if rnd.random() < 0.1 else "lander"
        lander = b.capture(f"{iid}-l", stage)
        caps.append(lander)
        if noise:
            vals = [("aio_landing", f"{prefix}n{i}.example", True), ("server_ip", f"203.0.113.{i % 250}", False)]
            if rnd.random() < 0.5:
                vals.append(("aio_cdn_tenant", f"{prefix}tn{i}.cdn.example", True))
        else:
            vals = [
                ("aio_cdn_tenant", op["tenant"], rnd.random() < 0.7),
                ("aio_owner", op["owner"], rnd.random() < 0.5),
                ("aio_landing", rnd.choice(op["landings"]), rnd.random() < 0.7),
                ("meta_pixel", rnd.choice(op["pixels"]), rnd.random() < 0.6),
                ("kit_asset_set", op["kit"], False),
                ("server_ip", op["ip"], False),
            ]
            vals = [v for v in vals if rnd.random() < 0.8]  # dropouts
            if chain:
                landing = next((v[1] for v in vals if v[0] == "aio_landing"), None)
                if landing is not None:
                    camp = op["landings"].index(landing) % 2
                    vals.append(("aio_campaign", f"{prefix}camp-{op['owner']}-{camp}", False))
        vals.append(("platform_name", "platform-x", False))  # a hub
        vals.append(("analyst_ip", "192.0.2.1", False))  # collector, never links
        if rnd.random() < 0.1:
            vals.append(("server_ip", "unknown", False))  # a failed lookup
        inds += [b.indicator(lander, k, v, live) for k, v, live in vals]
        if all_caps and rnd.random() < 0.08:
            # an exact duplicate of an earlier lander (a resubmitted ad)
            src = rnd.choice([c for c in all_caps if c.duplicate_of is None and c.id.endswith("-l")] or [all_caps[0]])
            dup = b.capture(f"{iid}-dup", "lander", duplicate_of=src.id)
            caps.append(dup)
        all_caps += caps
        out.append(Incident(id=iid, source="synthetic", captures=caps, indicators=inds))
    return out


def dataset(incidents: list[Incident], name: str = "synthetic") -> Dataset:
    return Dataset(name=name, incidents=incidents)


def replicate(n_incidents: int, seed: int = 11) -> Dataset:
    """A scale-up corpus: independent operator blocks, each with fresh invented values."""
    per = 50
    incidents: list[Incident] = []
    for blk in range((n_incidents + per - 1) // per):
        incidents += corpus(min(per, n_incidents - blk * per), n_operators=6, seed=seed + blk, prefix=f"b{blk}-")
    return dataset(incidents, name=f"synthetic-{n_incidents}")
