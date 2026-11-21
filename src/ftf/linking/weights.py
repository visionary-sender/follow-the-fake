"""Hand table of indicator weights for the tiered evidence ledger (design D1).

Every number here is an explicit, auditable prior in bits (log2 likelihood ratio), taken from the
method report (07 §2.5 and §3 D1): tenant id ~6, landing id ~4, live pixel ~3, kit asset set ~4,
caption ~2, platform constant 1.7, conflict on a single-valued field −4. HACK-23 replaces the table
with learned values; it is plain data on purpose, so a learned table only has to build a
`WeightTable` with the same fields.

Fields of one entry (`KindWeight`):

- `layer`: the layer the kind normally sits in. Scoring always uses the indicator's own layer
  (the contract is the truth); this field documents the expectation.
- `bits`: prior weight of one shared value, before the rarity correction.
- `single_valued`: the field takes one value per capture and is stable inside a campaign
  (platform, tenant, owner). Two different values give `conflict_bits`. Never set it for
  multi-valued fields such as pixels (a page fires several) or asset sets.
- `private`: an operator-private id (tenant, owner, tracker instance, tracking account). Shared
  *live* (seen in recorded traffic on both sides) it marks a strict link. Seen only in saved files
  it may come from a copied kit, so it loses `static_discount` bits.
- `relations`: per-relation scale in [0, 1]; `None` means the layer default in `RELATION_SCALE`.
  0 means the kind says nothing about that relation (no bits and no conflict penalty).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from ftf.contract.models import Layer, Relation

R = Relation
T, TA, OP, CA, EC = R.template, R.tracking_account, R.operator, R.campaign, R.ecosystem


def _row(template: float, tracking: float, operator: float, campaign: float, ecosystem: float) -> dict[Relation, float]:
    return {T: template, TA: tracking, OP: operator, CA: campaign, EC: ecosystem}


# How much each layer bears on each relation (columns: template, tracking_account, operator,
# campaign, ecosystem). Kit assets establish the template and only weakly the campaign; a pixel
# establishes the tracking account; platform, tenant and owner say nothing about the template
# (kits are copied across platforms).
RELATION_SCALE: dict[Layer, dict[Relation, float]] = {
    Layer.creative: _row(0.5, 0.0, 0.5, 1.0, 1.0),
    Layer.distribution_account: _row(0.0, 0.5, 1.0, 1.0, 0.5),
    Layer.tracking_account: _row(0.0, 1.0, 1.0, 1.0, 0.5),
    Layer.tracker_instance: _row(0.0, 1.0, 1.0, 1.0, 0.5),
    Layer.kit: _row(1.0, 0.0, 0.25, 0.5, 1.0),
    Layer.platform: _row(0.0, 0.0, 1.0, 1.0, 1.0),
    Layer.tenant: _row(0.0, 0.0, 1.0, 1.0, 1.0),
    Layer.owner: _row(0.0, 0.0, 1.0, 1.0, 1.0),
    Layer.landing: _row(1.0, 0.0, 1.0, 1.0, 1.0),
    Layer.offer: _row(0.0, 0.0, 0.5, 1.0, 1.0),
    Layer.infrastructure: _row(0.0, 0.0, 1.0, 1.0, 1.0),
    Layer.collector: _row(0.0, 0.0, 0.0, 0.0, 0.0),
}

# Relations in which a conflict on a single-valued field counts. Two tenants of one platform are
# still one ecosystem, and a copied kit on another platform is still one template.
CONFLICT_RELATIONS = frozenset({R.operator, R.campaign})


@dataclass(frozen=True)
class KindWeight:
    layer: Layer
    bits: float
    single_valued: bool = False
    private: bool = False
    relations: Optional[Mapping[Relation, float]] = None


@dataclass(frozen=True)
class WeightTable:
    kinds: Mapping[str, KindWeight]
    layer_defaults: Mapping[Layer, float]  # prior bits for kinds missing from `kinds`
    relation_scale: Mapping[Layer, Mapping[Relation, float]] = field(default_factory=lambda: RELATION_SCALE)
    conflict_relations: frozenset = CONFLICT_RELATIONS
    conflict_bits: float = -4.0
    hub_bits: float = 0.5  # a shared value worth less than this after rarity is hub_subsumed (0 bits)
    static_discount: float = 1.0  # private id seen only in saved files: possible copied kit

    def weight(self, kind: str, layer: Layer) -> KindWeight:
        """The entry for `kind`, or a non-private, multi-valued fallback at the layer default."""
        entry = self.kinds.get(kind)
        if entry is not None:
            return entry
        return KindWeight(layer=layer, bits=self.layer_defaults.get(layer, 0.0))

    def scale(self, kind: str, layer: Layer, relation: Relation) -> float:
        entry = self.kinds.get(kind)
        if entry is not None and entry.relations is not None:
            return entry.relations.get(relation, 0.0)
        return self.relation_scale.get(layer, {}).get(relation, 0.0)


_K = KindWeight
L = Layer

HAND_TABLE = WeightTable(
    kinds={
        # tracking account / tracker instance (operator-level, multi-valued)
        "meta_pixel": _K(L.tracking_account, 3.0, private=True),
        "google_ads_id": _K(L.tracking_account, 3.0, private=True),
        "capi_gateway": _K(L.tracking_account, 3.0, private=True),
        "xid_machine": _K(L.tracker_instance, 3.0, private=True),
        # platform chain: tenant → owner → landing
        "aio_cdn_tenant": _K(L.tenant, 6.0, single_valued=True, private=True),
        "aio_owner": _K(L.owner, 6.0, single_valued=True, private=True),
        "aio_landing": _K(L.landing, 4.0),
        # platform constants: one term for all of them (layer max), never a private id
        "platform_name": _K(L.platform, 1.7, single_valued=True),
        "aio_action_files": _K(L.platform, 1.7),
        "aio_sdk_asset": _K(L.platform, 1.7),
        "uniqueizer_hue_rotate": _K(L.platform, 1.7),
        # kit
        "kit_asset_set": _K(L.kit, 4.0),
        "aio_lander_template": _K(L.kit, 4.0),
        "aio_macros_file": _K(L.kit, 2.0),
        "brand_asset": _K(L.kit, 0.3),  # impersonated brand's logo/fonts: a stop-list hub
        # creative
        "ad_caption": _K(L.creative, 2.0),
        "page_title": _K(L.creative, 2.0),
        "image_phash": _K(L.creative, 3.0),  # creative pack (07 §2.5 b: s 0.3, u 0.05)
        # distribution account (pages rotate inside campaigns)
        "advertiser_page_name": _K(L.distribution_account, 2.0),
        "meta_campaign_id": _K(L.distribution_account, 4.0),
        # infrastructure (HACK-19 enrichment and prototype signals); shared hosting is weak
        "server_ip": _K(L.infrastructure, 1.5),
        "a_record_ip": _K(L.infrastructure, 1.5),
        "nameserver": _K(L.infrastructure, 1.5),
        "mx": _K(L.infrastructure, 1.5),
        "cert_san_sibling": _K(L.infrastructure, 2.0),
        "registrar": _K(L.infrastructure, 0.5),
        "registrar_iana_id": _K(L.infrastructure, 0.5),
        "ns_provider": _K(L.infrastructure, 0.3),
        "cert_issuer": _K(L.infrastructure, 0.0),
        # dates and flags describe a landing, they do not identify an actor
        "created_date": _K(L.infrastructure, 0.0),
        "domain_age_days_at_capture": _K(L.infrastructure, 0.0),
        "cert_first_seen": _K(L.infrastructure, 0.0),
        "cert_pl_listed": _K(L.infrastructure, 0.0),
        "cert_pl_listed_date": _K(L.infrastructure, 0.0),
    },
    layer_defaults={
        L.creative: 1.0,
        L.distribution_account: 2.0,
        L.tracking_account: 2.0,
        L.tracker_instance: 2.0,
        L.kit: 1.0,
        L.platform: 1.0,
        L.tenant: 4.0,
        L.owner: 4.0,
        L.landing: 3.0,
        L.offer: 1.0,
        L.infrastructure: 1.0,
        L.collector: 0.0,
    },
)

# Values that record a failed lookup, not evidence (HACK-19 writes `unknown` when a source is down).
IGNORED_VALUES = frozenset({"unknown"})
