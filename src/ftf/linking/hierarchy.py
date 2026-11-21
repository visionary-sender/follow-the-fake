"""Id hierarchy discovery and the chain rule (07 §2.2 items 2 and 5, §3 D2 "Data model").

The scammers' platform exposes a chain of ids: platform ⊃ tenant ⊃ owner ⊃ campaign ⊃ landing.
The *anchor* kinds name those levels directly (`ANCHOR_KINDS`). Every other value is placed at the
level it is a *constant* of (cluster-level document frequency): its carriers share one id of that
level and span at least two ids of the next finer level:

- one campaign, several landings → campaign level; one owner, several campaigns → operator; one
  tenant, several owners → tenant; a value inside one landing is no constant and keeps its weight;
- spanning ≥2 tenants (or ≥2 owners where no tenant is visible) → platform level (a shared
  main.js spanning three owners is a platform constant, a tenant id spanning two owners is a
  reseller's tenant, not an operator id);
- no carrier with any anchor id (other stacks) → no level; the chain rule leaves it alone.

Chain rule: when two captures share ids at several levels of one chain the evidence is one term
per level, Σ_levels log2(1/u_level|parent). The anchor rows are those terms. A non-anchor value
at level L counts 0 once level L is counted: by the shared L anchor if the pair shares one, else
by the best value at that level (one term per level). Platform-level values form one term, capped
at the platform layer's weight, however many platform constants match.

The level graph (`parents`) records, for each anchor level, the coarser anchor level its ids
determine (every id has exactly one parent id): it is reported, and documents why the chain sums.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ftf.contract.models import EvidenceRow, EvidenceStatus, Layer, Level

# Kinds that name a level of the aio chain directly (Ingestion's kinds, HACK-10).
ANCHOR_KINDS: dict[str, Level] = {
    "aio_cdn_tenant": Level.tenant,
    "aio_owner": Level.operator,
    "aio_campaign": Level.campaign,
    "aio_landing": Level.landing,
}
# Finest first: the order in which a value's carriers are checked for one shared anchor id.
CHAIN: tuple[Level, ...] = (Level.landing, Level.campaign, Level.operator, Level.tenant)
LEVEL_NAMES = {Level.platform: "platform", Level.tenant: "tenant", Level.operator: "owner",
               Level.campaign: "aio campaign", Level.landing: "landing"}

ValueKey = tuple[Layer, str, str]  # (layer, kind, normalised value)


@dataclass
class Hierarchy:
    level_of: dict[ValueKey, Level] = field(default_factory=dict)
    parents: dict[Level, Level | None] = field(default_factory=dict)  # level graph over anchor levels
    anchors: Mapping[str, Level] = field(default_factory=lambda: dict(ANCHOR_KINDS))

    @classmethod
    def from_captures(
        cls, captures: Mapping[str, Iterable[ValueKey]], anchors: Mapping[str, Level] = ANCHOR_KINDS
    ) -> "Hierarchy":
        """`captures`: canonical capture id → its (layer, kind, normalised value) keys (collector excluded)."""
        anchor_ids: dict[str, dict[Level, set[str]]] = {}
        carriers: dict[ValueKey, set[str]] = defaultdict(set)
        for cap, keys in captures.items():
            ids: dict[Level, set[str]] = defaultdict(set)
            for key in keys:
                carriers[key].add(cap)
                level = anchors.get(key[1])
                if level is not None:
                    ids[level].add(f"{key[1]}={key[2]}")
            anchor_ids[cap] = ids

        level_of: dict[ValueKey, Level] = {}
        for key, caps in carriers.items():
            if key[1] in anchors:
                level_of[key] = anchors[key[1]]
                continue
            level = _level(caps, anchor_ids)
            if level is not None:
                level_of[key] = level
        return cls(level_of=level_of, parents=_parents(anchor_ids), anchors=dict(anchors))

    @classmethod
    def from_dataset(cls, ds, anchors: Mapping[str, Level] = ANCHOR_KINDS) -> "Hierarchy":
        from ftf.linking.ledger import ScoringContext  # local: ledger imports this module

        return ScoringContext.from_dataset(ds, chain_rule=False).build_hierarchy(anchors)

    def level(self, key: ValueKey) -> Level | None:
        return self.level_of.get(key)

    def summary(self) -> dict:
        """Counts of values per (kind, level), for reports (never values)."""
        out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (_, kind, _), level in self.level_of.items():
            out[kind][LEVEL_NAMES.get(level, level.value)] += 1
        return {
            "level_graph": {LEVEL_NAMES[k]: (LEVEL_NAMES[v] if v else None) for k, v in self.parents.items()},
            "kinds": {k: dict(v) for k, v in sorted(out.items())},
        }


def _level(caps: set[str], anchor_ids: Mapping[str, Mapping[Level, set[str]]]) -> Level | None:
    """The level a value is a *constant* of: its carriers agree on one id of that level and span at least
    two ids of the next finer level present. A value inside one landing (or one id of the finest level
    its carriers show) is no constant: it stays its own evidence. No agreement at any level: platform."""
    union = {lv: set().union(*(anchor_ids[c].get(lv, set()) for c in caps)) for lv in CHAIN}
    present = [lv for lv in CHAIN if union[lv]]
    for i, lv in enumerate(present):
        if len(union[lv]) == 1:
            return lv if i > 0 and len(union[present[i - 1]]) >= 2 else None
    # no level agrees: a platform constant only if it spans several tenants, or several owners where no
    # tenant is visible; carriers that differ only in landing or campaign ids say nothing about the platform
    if len(union[Level.tenant]) >= 2 or (not union[Level.tenant] and len(union[Level.operator]) >= 2):
        return Level.platform
    return None


def _parents(anchor_ids: Mapping[str, Mapping[Level, set[str]]]) -> dict[Level, Level | None]:
    """For each anchor level, the finest coarser anchor level its ids determine (one parent id each)."""
    out: dict[Level, Level | None] = {}
    present = [lv for lv in CHAIN if any(ids.get(lv) for ids in anchor_ids.values())]
    for i, fine in enumerate(present):
        out[fine] = None
        for coarse in present[i + 1:]:
            parent_of: dict[str, set[str]] = defaultdict(set)
            for ids in anchor_ids.values():
                for f in ids.get(fine, ()):
                    parent_of[f] |= set(ids.get(coarse, ()))
            if parent_of and all(len(p) == 1 for p in parent_of.values()):
                out[fine] = coarse
                break
    return out


@dataclass
class ChainRow:
    """A shared evidence row before within-layer aggregation, with the value it rests on."""

    row: EvidenceRow
    bits: float
    key: ValueKey
    live_private: bool = False  # a live operator-private id (feeds the strict gate while it keeps bits)


def apply_chain_rule(cands: list[ChainRow], hierarchy: Hierarchy, platform_cap: float) -> tuple[list[ChainRow], list[EvidenceRow]]:
    """Split shared candidate rows into (kept, subsumed). Subsumed rows are `hub_subsumed`, 0 bits,
    with a note naming the level that explains them. Kept platform-level rows are capped at
    `platform_cap` bits."""
    by_level: dict[Level, list[ChainRow]] = defaultdict(list)
    kept: list[ChainRow] = []
    for c in cands:
        level = hierarchy.level(c.key)
        if level is None:
            kept.append(c)
        else:
            by_level[level].append(c)

    subsumed: list[EvidenceRow] = []
    for level, rows in by_level.items():
        name = LEVEL_NAMES.get(level, level.value)
        anchors = [c for c in rows if hierarchy.anchors.get(c.key[1]) == level]
        others = [c for c in rows if hierarchy.anchors.get(c.key[1]) != level]
        if anchors:
            kept += anchors
            why = f"explained by the shared {name} id ({anchors[0].key[1]})"
            subsumed += [_subsume(c, why) for c in others]
            continue
        if level is Level.platform:
            # one term for all platform constants; prefer a platform-layer row, then the strongest
            others.sort(key=lambda c: (c.key[0] is not Layer.platform, -c.bits, c.key[1]))
        else:
            others.sort(key=lambda c: (-c.bits, c.key[1]))
        top, rest = others[0], others[1:]
        if level is Level.platform and 0 < platform_cap < top.bits:
            top = ChainRow(top.row.model_copy(update={
                "bits": round(platform_cap, 3),
                "note": f"{top.row.note}; platform-level value, capped at the platform term {platform_cap:g}",
            }), round(platform_cap, 3), top.key, top.live_private)
        kept.append(top)
        subsumed += [_subsume(c, f"{name}-level value, the {name} is already counted by {top.key[1]}") for c in rest]
    return kept, subsumed


def _subsume(c: ChainRow, why: str) -> EvidenceRow:
    return c.row.model_copy(update={
        "status": EvidenceStatus.hub_subsumed, "bits": 0.0, "note": f"{c.row.note}; chain rule: {why}",
    })
