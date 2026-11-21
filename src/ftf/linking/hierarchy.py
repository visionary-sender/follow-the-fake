"""Id hierarchy discovery and the chain rule (07 §2.2 items 2 and 5, §3 D2 "Data model").

The scammers' platform exposes a chain of ids: platform ⊃ tenant ⊃ owner ⊃ campaign ⊃ landing.
The *anchor* kinds name those levels directly (`ANCHOR_KINDS`). Every other value is placed at the
level it is a *constant* of (cluster-level document frequency): its carriers share one id of that
level and span at least two ids of the next finer level:

- one campaign, several landings → campaign level; one owner, several campaigns → operator; one
  tenant, several owners → tenant; a value inside one landing is no constant and keeps its weight;
- no level agreeing, spanning ≥3 tenants (≥2 for platform- and kit-layer kinds; owners where no tenant
  is visible) → platform level; operator-private kinds are never demoted to platform (a shared
  main.js spanning three owners is a platform constant, a tenant id spanning two owners is a
  reseller's tenant, not an operator id);
- a carrier without any chain id (another stack) → no level; the chain rule leaves it alone.

Chain rule: when two captures share ids at several levels of one chain the evidence is one term
per level, Σ_levels log2(1/u_level|parent): the strongest of the level's shared id and the values
that are constants of that level; the others count 0. Platform-level values form one term, capped
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
            level = _level(key, caps, anchor_ids)
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


def _level(key: ValueKey, caps: set[str], anchor_ids: Mapping[str, Mapping[Level, set[str]]]) -> Level | None:
    """The level a value is a *constant* of, or None.

    Only levels that *every* carrier shows count (a carrier without chain ids, e.g. a page of another
    stack, makes the value no constant of the chain). The value is a constant of the finest such
    level on which all carriers agree, if they span at least two ids of the next finer such level.
    It is a platform constant if no such level agrees and it spans ≥3 tenants (≥2 for platform- and
    kit-layer kinds, the platform's own code), or the same in owners where no tenant is visible.
    """
    if any(not any(anchor_ids[c].values()) for c in caps):
        return None
    shown = [lv for lv in CHAIN if all(anchor_ids[c].get(lv) for c in caps)]
    union = {lv: set().union(*(anchor_ids[c][lv] for c in caps)) for lv in shown}
    for i, lv in enumerate(shown):
        if len(union[lv]) == 1:
            return lv if i > 0 and len(union[shown[i - 1]]) >= 2 else None
    need = 2 if key[0] in (Layer.platform, Layer.kit) else 3
    if Level.tenant in shown:
        return Level.platform if len(union[Level.tenant]) >= need else None
    if Level.operator in shown:
        return Level.platform if len(union[Level.operator]) >= need else None
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
    private: bool = False  # an operator-private kind (never demoted to a platform constant)


def apply_chain_rule(cands: list[ChainRow], hierarchy: Hierarchy, platform_cap: float) -> tuple[list[ChainRow], list[EvidenceRow]]:
    """Split shared candidate rows into (kept, subsumed): one term per chain level, the strongest of the
    level's shared id and its constants. Subsumed rows are `hub_subsumed`, 0 bits, with a note naming the
    level. The platform term is capped at `platform_cap` and never opens the live-private gate.
    Operator-private ids are never demoted to platform constants."""
    by_level: dict[Level, list[ChainRow]] = defaultdict(list)
    kept: list[ChainRow] = []
    for c in cands:
        level = hierarchy.level(c.key)
        if level is None or (level is Level.platform and c.private):
            kept.append(c)
        else:
            by_level[level].append(c)

    subsumed: list[EvidenceRow] = []
    for level, rows in by_level.items():
        name = LEVEL_NAMES.get(level, level.value)
        if level is Level.platform:
            # prefer a platform-layer row, then the strongest
            rows.sort(key=lambda c: (c.key[0] is not Layer.platform, -c.bits, c.key[1]))
        else:
            # the strongest; on a tie the level's own id
            rows.sort(key=lambda c: (-c.bits, hierarchy.anchors.get(c.key[1]) != level, c.key[1]))
        top, rest = rows[0], rows[1:]
        if level is Level.platform:
            row, bits = top.row, top.bits
            if 0 < platform_cap < bits:
                row = row.model_copy(update={
                    "bits": round(platform_cap, 3),
                    "note": f"{row.note}; platform-level value, capped at the platform term {platform_cap:g}",
                })
                bits = round(platform_cap, 3)
            top = ChainRow(row, bits, top.key, live_private=False, private=top.private)
        kept.append(top)
        subsumed += [_subsume(c, f"one term per {name}, counted by {top.key[1]}") for c in rest]
    return kept, subsumed


def _subsume(c: ChainRow, why: str) -> EvidenceRow:
    return c.row.model_copy(update={
        "status": EvidenceStatus.hub_subsumed, "bits": 0.0, "note": f"{c.row.note}; chain rule: {why}",
    })
