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
        return KindWeight(layer=layer, bits=self.layer_defaults.get(layer, 0.0), private=layer in PRIVATE_LAYERS)

    def scale(self, kind: str, layer: Layer, relation: Relation) -> float:
        entry = self.kinds.get(kind)
        if entry is not None and entry.relations is not None:
            return entry.relations.get(relation, 0.0)
        return self.relation_scale.get(layer, {}).get(relation, 0.0)


_K = KindWeight
L = Layer

HAND_TABLE = WeightTable(
    kinds={
        # Ingestion kinds (HACK-10, `ftf.indicators`). Hierarchy: platform ⊃ tenant ⊃ owner ⊃
        # aio campaign ⊃ landing. Layers are Ingestion's; within a layer only the best kind counts.
        # tracking account / tracker instance (07 §2.5 a: live pixel ~3; multi-valued, a page fires several)
        "meta_pixel": _K(L.tracking_account, 3.0, private=True),  # ad account's pixel
        "capi_gateway": _K(L.tracking_account, 3.0, private=True),  # server-side pixel gateway of one account
        "google_ads_id": _K(L.tracking_account, 3.0, private=True),  # ads conversion account
        "google_analytics_id": _K(L.tracking_account, 3.0, private=True),  # analytics property
        "clarity_id": _K(L.tracking_account, 3.0, private=True),  # session-recording project
        "push_client_id": _K(L.tracker_instance, 3.0, private=True),  # push-service client: one tracker instance
        "xid_tracker_machine": _K(L.tracker_instance, 3.0, private=True),  # self-hosted tracker machine id
        "tracker_postback": _K(L.tracker_instance, 3.0, private=True),  # conversion postback of one tracker
        # platform chain: tenant → owner → aio campaign → landing (07 §2.2 item 5)
        "aio_cdn_tenant": _K(L.tenant, 6.0, single_valued=True, private=True),  # one tenant per page
        "aio_handle_key": _K(L.tenant, 5.0, private=True),  # account handle key; multi-valued until proven stable
        # flow: template-level config (sold/copied with the kit): full template, weak campaign
        "aio_flow": _K(L.tenant, 3.0, relations=_row(1.0, 0.0, 0.25, 0.25, 1.0)),
        "aio_owner": _K(L.owner, 6.0, single_valued=True, private=True),  # one owner per page
        # campaign id sits in Ingestion's owner layer, so it only matters when the owner id is absent;
        # not single-valued: one owner runs several campaigns, and a differing campaign is no cannot-link
        "aio_campaign": _K(L.owner, 5.0, private=True),
        "aio_campaign_name": _K(L.owner, 3.0),  # owner-chosen label, names repeat more than ids
        "aio_landing": _K(L.landing, 4.0),  # landing id; multi-valued (a capture can show several)
        "aio_landing_name": _K(L.landing, 3.0),  # label of the landing, weaker than its id
        # platform constants: one term for all of them (layer max), never a private id
        "platform_name": _K(L.platform, 1.7, single_valued=True),
        "aio_action_files": _K(L.platform, 1.7),  # platform's own scripts, identical on every tenant
        "aio_sdk_asset": _K(L.platform, 1.7),  # platform SDK files, identical on every tenant
        "uniqueizer_hue_rotate": _K(L.platform, 1.7),
        # distribution: traffic source configured in the platform; a label, operator-level
        "aio_source": _K(L.distribution_account, 2.0),
        # offer: operator-private contact data; the offer layer's default scale halves operator,
        # these say as much about operator as about campaign
        "contact_email": _K(L.offer, 4.5, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "contact_phone": _K(L.offer, 4.5, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        # lead endpoint: Ingestion marks it platform-level (several owners can post to one
        # endpoint), so not private (a shared live endpoint must not alone make a strict link)
        "form_endpoint": _K(L.offer, 4.0, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "form_field_set": _K(L.offer, 1.0, relations=_row(1.0, 0.0, 0.25, 0.5, 1.0)),  # kit-like, low
        "aio_form": _K(L.offer, 1.0),  # form template of the platform, near-constant
        # video host tenant: tenant-like but host-side (infrastructure layer), multi-valued
        "video_cdn_tenant": _K(L.infrastructure, 4.0, private=True),
        # kit
        # A whole identical asset set (07 §2.5 c: s 0.9, u 0.02 → template +5.5 after one bit of
        # rarity). Deliberate deviation from 07, which counts it fully toward campaign: here it
        # counts at 0.8, so a copied kit on another platform (−4 conflict) lands in campaign
        # review, never in link, and a same-platform copy still needs a second layer.
        "kit_asset_set": _K(L.kit, 6.5, relations=_row(1.0, 0.0, 0.25, 0.8, 1.0)),
        # lander template: a template-level id, counts fully for template, weakly for campaign
        "aio_lander_template": _K(L.kit, 4.0, relations=_row(1.0, 0.0, 0.25, 0.25, 1.0)),
        "aio_macros_file": _K(L.kit, 2.0),
        # HACK-11 kit fingerprints. One asset hash can be a landing build or a whole platform's
        # SDK; rarity over pre-groups demotes the platform-wide ones, so the prior stays modest.
        "asset_sha256": _K(L.kit, 2.0),  # one identical asset file
        "asset_norm_sha256": _K(L.kit, 2.0),  # same asset after re-minification
        "inline_script_sha256": _K(L.kit, 2.0),  # normalised inline script, per-deployment literals masked
        "dom_skeleton_sha256": _K(L.kit, 3.0),  # whole-page tag skeleton: one page build (≥30 tags)
        "dom_skeleton_lsh": _K(L.kit, 1.0),  # one MinHash band of a near-identical skeleton; one band counts
        # URL parameter shapes (values dropped): platform-level structure, template-ish, weak
        "page_url_shape": _K(L.landing, 1.0, relations=_row(1.0, 0.0, 0.5, 0.5, 1.0)),
        "form_action_shape": _K(L.landing, 1.0, relations=_row(1.0, 0.0, 0.5, 0.5, 1.0)),
        "brand_asset": _K(L.kit, 0.3),  # impersonated brand's logo/fonts: a stop-list hub
        # creative
        "ad_caption": _K(L.creative, 2.0),
        "page_title": _K(L.creative, 2.0),
        "image_phash": _K(L.creative, 3.0),  # creative pack (07 §2.5 b: s 0.3, u 0.05)
        "meta_ad_id": _K(L.creative, 4.0),  # one ad; rarely shared, but a shared one is the same ad
        "meta_ad_name": _K(L.creative, 2.0),  # free text like a caption
        # distribution account (pages rotate inside campaigns; 07 §(e) item 10)
        "advertiser_page_name": _K(L.distribution_account, 2.0),
        "meta_campaign_id": _K(L.distribution_account, 4.0),  # ad-manager campaign of one account
        "meta_adset_id": _K(L.distribution_account, 4.0),  # ad set inside that campaign
        "meta_campaign_name": _K(L.distribution_account, 2.0),  # account-chosen label
        "meta_adset_name": _K(L.distribution_account, 2.0),  # account-chosen label
        # infrastructure (HACK-19 enrichment and prototype signals); shared hosting is weak
        "third_party_host": _K(L.infrastructure, 1.5),  # HACK-11: foreign host a page loads; CDNs are hubs
        "third_party_domain": _K(L.infrastructure, 1.0),  # HACK-11: its registered domain, coarser
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

# Layers whose unknown kinds are treated as operator-private ids (fallback in `WeightTable.weight`).
PRIVATE_LAYERS = frozenset({Layer.tenant, Layer.owner, Layer.tracker_instance, Layer.tracking_account})
