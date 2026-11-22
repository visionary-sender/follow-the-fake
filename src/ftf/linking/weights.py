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
#
# Tracking accounts and tracker instances stay at 1.0 for campaign (HACK-53), although one pixel or
# CAPI gateway spans several campaigns of one owner (07 §2.2, §2.3). Separating those campaigns is the
# job of the campaign gate (`CAMPAIGN_EVIDENCE_*` below, applied in `ledger.score_pair`), not of the
# scale: on the organiser set E1, E7 and the campaign/operator P/R counts were identical at 0.5, 0.8
# and 1.0 once the gate was on, while any scale below ~0.94 drops the static pixel of 07 §2.5 (c)
# (a copied kit) under hub_bits and turns that worked example from campaign review into no link.
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
        "tiktok_pixel": _K(L.tracking_account, 3.0, private=True),  # ad account's pixel on another network
        "push_client_id": _K(L.tracking_account, 3.0, private=True),  # push-service client of one operator
        # analytics ids: a lander that is a saved copy of a real outlet's article fires that outlet's
        # analytics, so a shared one must not alone make a strict link (not private)
        "google_analytics_id": _K(L.tracking_account, 3.0),  # analytics property
        "gtm_container": _K(L.tracking_account, 3.0),  # tag-manager container
        "clarity_id": _K(L.tracking_account, 3.0),  # session-recording project
        "yandex_metrica_id": _K(L.tracking_account, 3.0),  # analytics counter
        "xid_tracker_machine": _K(L.tracker_instance, 3.0, private=True),  # self-hosted tracker machine id
        "tracker_postback": _K(L.tracker_instance, 3.0, private=True),  # conversion postback of one tracker
        # platform chain: tenant → owner → aio campaign → landing (07 §2.2 item 5)
        "aio_cdn_tenant": _K(L.tenant, 6.0, single_valued=True, private=True),  # one tenant per page
        "aio_handle_key": _K(L.tenant, 5.0, private=True),  # account handle key; multi-valued until proven stable
        # flow: template-level config (sold/copied with the kit): full template, weak campaign
        "aio_flow": _K(L.tenant, 3.0, relations=_row(1.0, 0.0, 0.25, 0.25, 1.0)),
        "aio_owner": _K(L.owner, 6.0, single_valued=True, private=True),  # one owner per page
        # campaign id (Ingestion files it in the owner layer, level campaign): one per page, so a differing
        # id under one owner is a different campaign of the same operator. Single-valued and campaign-only:
        # a conflict row under the campaign relation, nothing under operator, and never a cannot-link
        # (cannot-link stays tenant/owner ids, `ledger.has_private_conflict`).
        "aio_campaign": _K(L.owner, 4.0, single_valued=True, private=True, relations=_row(0.0, 0.0, 0.0, 1.0, 1.0)),
        # owner-chosen label of the campaign: names repeat more than ids, so no conflict; campaign-only too
        "aio_campaign_name": _K(L.owner, 3.0, relations=_row(0.0, 0.0, 0.0, 1.0, 1.0)),
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
        # these say as much about operator as about campaign. Kept below 07's ~4.5 because an
        # impersonating page can show the real brand's hotline or footer email (a hub)
        "contact_email": _K(L.offer, 3.5, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "contact_phone": _K(L.offer, 3.5, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "contact_telegram": _K(L.offer, 3.5, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "aio_advertiser": _K(L.offer, 3.0, private=True, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),  # advertiser id in the platform
        "offer_id": _K(L.offer, 3.0),  # offer id of an affiliate network: campaign-level, shared by affiliates
        # lead endpoint: a normalised path (no host), the same on every lander of one platform or
        # kit, so a weak template hint rather than an operator id; not private
        "form_endpoint": _K(L.offer, 2.0, relations=_row(1.0, 0.0, 0.25, 0.5, 1.0)),
        "form_field_set": _K(L.offer, 2.0, relations=_row(1.0, 0.0, 0.25, 0.5, 1.0)),  # kit-like form fields
        # near-constant form template of the platform: display only (rarity keeps it below hub_bits)
        "aio_form": _K(L.offer, 1.0),
        # video host tenant: tenant-like but host-side (infrastructure layer), multi-valued; not
        # private, since a copied article can hotlink the real outlet's video zone
        "video_cdn_tenant": _K(L.infrastructure, 4.0),
        # kit
        # A whole identical asset set (07 §2.5 c: s 0.9, u 0.02 → template +5.5 after one bit of
        # rarity). Deliberate deviation from 07, which counts it fully toward campaign: here it
        # counts at 0.8, so a copied kit on another platform (−4 conflict) lands in campaign
        # review, never in link, and a same-platform copy still needs a second layer.
        "kit_asset_set": _K(L.kit, 6.5, relations=_row(1.0, 0.0, 0.25, 0.8, 1.0)),
        # lander template: a template-level id, counts fully for template, weakly for campaign
        "aio_lander_template": _K(L.kit, 4.0, relations=_row(1.0, 0.0, 0.25, 0.25, 1.0)),
        "aio_macros_file": _K(L.kit, 2.0),
        # HACK-11 kit fingerprints: template evidence (kit scale: template 1, campaign 0.5). One asset
        # hash can be a landing build or a whole platform's SDK; rarity over pre-groups demotes the
        # platform-wide ones. Priors are set so a pair-only value (k = 2) still clears hub_bits.
        "asset_sha256": _K(L.kit, 2.5),  # one identical asset file
        "asset_norm_sha256": _K(L.kit, 2.5),  # same asset after re-minification
        "inline_script_sha256": _K(L.kit, 3.0),  # normalised inline script, per-deployment literals masked
        "dom_skeleton_sha256": _K(L.kit, 3.5),  # whole-page tag skeleton: one page build (≥30 tags)
        "dom_skeleton_lsh": _K(L.kit, 2.0),  # one MinHash band of a near-identical skeleton; one band counts
        # URL parameter shapes (values dropped): structure of the kit or tracker, template-ish, weak
        "page_url_shape": _K(L.landing, 2.0, relations=_row(1.0, 0.0, 0.25, 0.5, 1.0)),
        "form_action_shape": _K(L.landing, 2.0, relations=_row(1.0, 0.0, 0.25, 0.5, 1.0)),
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
        "third_party_domain": _K(L.infrastructure, 1.5),  # HACK-11: its registered domain, coarser
        # site identity (HACK-48, final names on HACK-10): in the landing layer, so the within-layer max keeps
        # "same site" from stacking with the landing id; not single-valued (domains rotate daily: a differing
        # domain carries nothing, no conflict penalty) and nothing about the template. landing_domain also
        # carries the CERT listing domain. ad_display_domain (creative layer) only restates it: kept weak.
        "landing_host": _K(L.landing, 3.0, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "landing_domain": _K(L.landing, 2.5, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "ad_landing_domain": _K(L.landing, 2.5, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        "ad_display_domain": _K(L.creative, 1.0, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
        # redirectors and trackers on the way (tracker_instance when the URL looks like a click tracker, else
        # infrastructure; scoring uses the indicator's own layer): can be a hub shared by unrelated operators,
        # so not private; pre-group rarity does the rest. The chain shape sits in the landing layer.
        "chain_host": _K(L.tracker_instance, 2.0),
        "chain_domain": _K(L.tracker_instance, 2.0),
        "redirect_chain_shape": _K(L.landing, 1.0, relations=_row(0.0, 0.0, 1.0, 1.0, 1.0)),
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

# Campaign-level evidence (HACK-53): what one campaign has and another campaign of the same owner does not.
# Under the campaign relation the D1 gate needs at least one shared row of this kind (see `ledger.score_pair`):
# the operator's tracking accounts, tracker instances, kit, platform, tenant, owner, offer contacts and
# shared hosting are common to all of that operator's campaigns. Campaign-level: the creative and the ad
# (ad id, caption, title, image), the ad account's campaign and ad set, the landing (aio landing id, the
# scam's own host or domain), and the aio campaign id or name and an affiliate offer id. Excluded from those
# layers: URL and form shapes and the redirect-chain shape (template-like structure), and the aio traffic
# source label (operator-level).
CAMPAIGN_EVIDENCE_LAYERS = frozenset({Layer.creative, Layer.distribution_account, Layer.landing})
CAMPAIGN_EVIDENCE_KINDS = frozenset({"aio_campaign", "aio_campaign_name", "offer_id"})
NOT_CAMPAIGN_EVIDENCE_KINDS = frozenset({"aio_source", "page_url_shape", "form_action_shape", "redirect_chain_shape"})


def is_campaign_evidence(layer: Layer, kind: str) -> bool:
    """A shared value of this (layer, kind) tells one campaign from another of the same operator."""
    if kind in NOT_CAMPAIGN_EVIDENCE_KINDS:
        return False
    return kind in CAMPAIGN_EVIDENCE_KINDS or layer in CAMPAIGN_EVIDENCE_LAYERS


# Values that record a failed lookup, not evidence (HACK-19 writes `unknown` when a source is down).
IGNORED_VALUES = frozenset({"unknown"})

# Layers whose unknown kinds are treated as operator-private ids (fallback in `WeightTable.weight`).
PRIVATE_LAYERS = frozenset({Layer.tenant, Layer.owner, Layer.tracker_instance, Layer.tracking_account})


def unknown_kinds(ds, table: WeightTable = HAND_TABLE) -> dict[str, int]:
    """Linkable kinds in `ds` without an explicit entry in `table` (they use the layer fallback), with counts."""
    from collections import Counter

    from ftf.contract.models import linkable

    counts = Counter(i.kind for inc in ds.incidents for i in inc.indicators if linkable(i) and i.kind not in table.kinds)
    return dict(sorted(counts.items()))


def warn_unknown_kinds(ds, table: WeightTable, prog: str) -> None:
    """One stderr line naming the kinds scored with the per-layer fallback (bounded by the within-layer max)."""
    import sys

    found = unknown_kinds(ds, table)
    if found:
        listed = ", ".join(f"{k} ({n})" for k, n in found.items())
        print(f"ftf {prog}: warning: kinds without a weight entry use the layer fallback: {listed}", file=sys.stderr)
