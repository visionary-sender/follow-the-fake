# Methodology: from one capture to a campaign

This describes how Follow the Fake turns a suspicious ad or landing page into evidence, and how
that evidence groups captures into campaigns. It is written for NASK analysts. Every number
below is read from the code (`src/ftf/linking/`), and every weight is an explicit, hand-set prior
in bits, not a value learned from the challenge data. Read section 9 before quoting a score.

Terms used throughout: a **capture** is one visit or one saved item (an ad, a pre-lander, a
landing page, a decoy page); an **incident** is one submitted report and holds its captures; an
**indicator** is one extracted identifier or feature of a capture; a **link** is a scored pair of
captures; a **campaign** is a group of captures under one relation. The data contract is in
[contract.md](contract.md).

## 1. Input

The system takes four kinds of input, and each arrives as a contract incident:

- **Case folders** (the challenge data, read by ingestion): saved ads, saved pages (HTML, MHTML,
  HAR), screenshots and metadata. Folder numbers name the cases in this document.
- **A URL** fetched by the safe fetcher (`ftf fetch`). The fetch runs in a sandbox; the fetcher
  records what a visitor would receive (HAR, final DOM, screenshot, redirect chain) and never
  submits forms.
- **Ad-library records** found by the scanner (`ftf scan`): library id, advertiser, ad text,
  declared landing URL and creative links. The scanner never opens a landing page.
- **A keyword, an ad screenshot or a base item** given to `ftf analyze`.

The analysis never executes or renders captured files: HTML, scripts and PDFs are read as inert text. Only the sandboxed fetcher loads a live page, in a locked-down browser.

## 2. Captures (the contract)

Each capture belongs to one incident and has a stage (`ad`, `pre_lander`, `lander`, `decoy` or
`unknown`), a capture day, the artefacts it holds (`ad_screenshot`, `screenshot`, `html`, `har`,
`mhtml`, `video`, `url`, `text`, `asset`, `ad_library`, `other`) and an explicit list of modalities
that are **missing**, each with a reason. Exact duplicates point to the capture they collapse onto
(`duplicate_of`), so a duplicate adds neither evidence nor rarity. A decoy page shows the cloaker's
"white page" to visitors it does not want, so it never joins the campaign of its incident on its
own.

## 3. Indicators per layer

An indicator is one identifier or feature, stored with its **layer** (what aspect of the scam it
describes), its **level** (the granularity of the entity it names) and whether it was seen in
recorded traffic (`live`) or in saved files (`static`). Values from the same layer are treated as
one decision by one actor, so a layer counts once when it is combined with other layers (section 5).

| Layer | Example kinds | Level | Live or static | Where it comes from |
|---|---|---|---|---|
| `creative` | `ad_caption`, `ad_title`, `ad_display_domain`, `meta_ad_id`, `meta_ad_name`, `image_phash` | campaign, capture | static | Ad-library records (scanner), saved creatives; `image_phash` is planned (content features, below) |
| `distribution_account` | `meta_campaign_id`, `meta_adset_id`, `advertiser_page_name`, `aio_source` | operator, campaign | static, live for ids in requests | ad-manager ids in ad-library records and in page code |
| `tracking_account` | `meta_pixel`, `capi_gateway`, `google_ads_id`, `google_analytics_id`, `gtm_container`, `tiktok_pixel`, `clarity_id`, `push_client_id` | operator (analytics ids: campaign-level weight) | live when in recorded traffic, else static | HAR requests and saved page code (`ftf.indicators.platform`) |
| `tracker_instance` | `xid_tracker_machine`, `tracker_postback`, `chain_host`, `chain_domain` (click-tracker hops) | operator | live | decoded click ids and conversion postbacks in traffic |
| `kit` | `asset_sha256`, `asset_norm_sha256`, `inline_script_sha256`, `dom_skeleton_sha256`, `dom_skeleton_lsh`, `aio_lander_template` | landing, platform | static | Hashes of saved assets, normalised inline scripts and the page's tag skeleton (`ftf.indicators.kit`) |
| `platform` | `platform_name`, `aio_sdk_asset`, `aio_action_files`, `uniqueizer_hue_rotate` | platform | static | Constants that a platform injects into every page it hosts |
| `tenant` | `aio_cdn_tenant` (single-valued), `aio_handle_key`, `aio_flow` | tenant, campaign | live or static | Account and tenant configuration in page code and traffic |
| `owner` | `aio_owner` (single-valued), `aio_campaign`, `aio_campaign_name` | operator, campaign | live or static | Operator configuration in page code and traffic |
| `landing` | `aio_landing`, `aio_landing_name`, `landing_host`, `landing_domain`, `ad_landing_domain`, `redirect_chain_shape`, `page_url_shape` | landing | live or static | Landing ids in paths and traffic; URL shapes with values removed |
| `offer` | `contact_email`, `contact_phone`, `contact_telegram`, `aio_advertiser`, `offer_id`, `form_endpoint`, `form_field_set` | operator, campaign | static, live for form posts | Contact data in page code, form endpoints and field sets, affiliate offer ids |
| `infrastructure` | `third_party_host`, `third_party_domain`, `chain_domain` (cloaker hops), `registrar`, `nameserver`, `a_record_ip`, `mx`, `cert_san_sibling`, `cert_issuer`, `created_date` | platform, operator, tenant, landing | live (a lookup or a cache of one) | Third-party hosts in the page (`ftf.indicators.kit`); registry, DNS and certificate lookups (`ftf.enrich`, see [enrichment.md](enrichment.md)) |
| `collector` | `aio_fingerprint`, `browser_fingerprint`, `analyst_ip`, `analyst_geo`, `user_agent`, `viewport`, `screen_size`, `fbclid_session`, `local_path`, `os_username`, `capture_time`, `capture_day`, `fetch_outcome` | capture | either | Describes the analyst or the capture tool, never the scam (section 4) |

Kinds and their layers are declared in `ftf.indicators._ids.KINDS`, `ftf.indicators.kit._KINDS`,
`ftf.indicators.domains` (the landing's own hosts, the redirect chain and the displayed domain of an
ad, HACK-48; the page's own URLs come from `ftf.indicators._pages`) and `ftf.enrich.domain`. The extractors read files as inert text and never fetch a URL found in a
capture. The content layers (text embeddings, image hashes, faces) count only through calibrated
content views, which are off by default (5.6); without them the scores below use the identifier,
kit, infrastructure and ad-text indicators.

## 4. Collector exclusion

A value that describes the person or tool that captured a page says nothing about the scam. It
would link everything one analyst collected, so it is excluded **by type, never by frequency**:
the kinds in `COLLECTOR_KINDS` must use the `collector` layer, and `linkable()` is false for that
layer. A shared collector value appears in the ledger as `collector_excluded` with 0 bits, so the
reader can see that it was there and that it was ignored. `tests/linking/test_ledger.py` checks that
collector traces change no score, and `tests/linking/test_grouping.py` that they create no candidate pair.
The stronger check, injecting one shared fingerprint, address and capture day into two unrelated
cases and requiring that no decision changes, is part of the evaluation protocol and is not yet run.

## 5. Evidence in bits

### 5.1 The weight of one shared value

A shared value is evidence because it is rarer in other scams than it would be if the two captures
belonged to different campaigns. Fellegi and Sunter's match weight gives the form. For a value `v`
of a kind in a layer:

    bits(v) = log2[(s + (1 − s)·u) / u]

Here `s` is the **persistence** (how likely the attacker keeps the value inside one campaign) and
`u` is the **background frequency** (how likely the value is in a scam that is not this campaign).
Both are priors in the code. The code writes the same weight as `prior × relation scale`, and then
subtracts the rarity term:

    bits = max(max(prior × scale − static discount, 0) − log2(k), 0)

- `prior` is the kind's bits in `ftf.linking.weights.HAND_TABLE`, for example `aio_cdn_tenant` 6.0,
  `aio_owner` 6.0, `aio_landing` 4.0, `kit_asset_set` 6.5, `meta_pixel` 3.0, `ad_caption` 2.0,
  `platform_name` 1.7, `third_party_host` 1.5, `registrar` 0.5.
- `scale` is how much the layer bears on the relation being scored (table below).
- `static discount` is 1.0 bit for an operator-private id (tenant, owner, tracker, tracking
  account) not live on both sides (in saved files, or seen in one capture's traffic only), because a
  copied kit can carry the original's ids. A live id seen on both sides has no discount.
- `k` is the **number of pre-groups** that carry the value, not the number of incidents. Incidents
  that share a live operator-private id are joined into one pre-group first, so a large campaign's
  own identifiers are not penalised for the campaign being large (`ftf.linking.ledger._pre_groups`).
  An incident with no such id is its own group.
- A value whose weight after rarity is below `hub_bits` = 0.5 bit is a **hub**. It is kept in the
  ledger as `hub_subsumed` with 0 bits, because a value that common says nothing about one campaign.
- With a background corpus (`ftf link --background`), the rarity term takes corpus counts for the
  values the corpus knows: `log2[(c + α)/(1 + α)]`, where `c` is the larger of the corpus operator count
  and the sample's pre-groups (`ftf.linking.background`).
  Without a corpus, the pre-group count above is used. The default run has no corpus.
- **Brand stop-list (on by default, HACK-66).** A scam clone loads the impersonated outlet's logo,
  fonts, favicon, CDN and analytics. Those values are the outlet's skin, not the scammer's. `ftf
  brand-stoplist` hashes every value seen on the real outlet pages of the background corpus (tagged
  `must_not_link`). The default scoring path reads that file from `$FTF_BRAND_STOPLIST`, or from
  `/data/derived/linking/brand-stoplist/brand-stoplist.json` when the variable is not set. A
  stop-listed value is `hub_subsumed` with 0 bits under every relation except ecosystem. The
  `brand_asset` kind counts under ecosystem only, with or without the list. Without the file (the jury
  image ships it, HACK-75), the engine prints a warning and scores as if no stop-list existed.
  The file holds digests only.
- A value carried by more than 500 captures never proposes a candidate pair (`HUB_POSTINGS`).
- The priors above are the **hand table**. `ftf link --weights FILE` replaces them with a learned table
  (HACK-23, `ftf.linking.learned`), which keeps the same fields. The default run uses the hand table.

### 5.2 How layers are scored against a relation

Layers bear on different relations to different degrees. The scale table is `RELATION_SCALE` in
`ftf.linking.weights`; 0 means the layer says nothing about that relation. A few kinds override their
layer's row (for example `kit_asset_set` counts 0.8 for the campaign, not 0.5; see the table in
the code), and the override wins.

| Layer | same template | same tracking account | same operator | same campaign | same ecosystem |
|---|---:|---:|---:|---:|---:|
| `creative` | 0.5 | 0 | 0.5 | 1 | 1 |
| `distribution_account` | 0 | 0.5 | 1 | 1 | 0.5 |
| `tracking_account` | 0 | 1 | 1 | 1 | 0.5 |
| `tracker_instance` | 0 | 1 | 1 | 1 | 0.5 |
| `kit` | 1 | 0 | 0.25 | 0.5 | 1 |
| `platform` | 0 | 0 | 1 | 1 | 1 |
| `tenant` | 0 | 0 | 1 | 1 | 1 |
| `owner` | 0 | 0 | 1 | 1 | 1 |
| `landing` | 1 | 0 | 1 | 1 | 1 |
| `offer` | 0 | 0 | 0.5 | 1 | 1 |
| `infrastructure` | 0 | 0 | 1 | 1 | 1 |
| `collector` | 0 | 0 | 0 | 0 | 0 |

A kit establishes a *template* (the same page build, which can be sold or copied) but only weakly
an operator, because copied kits sit on other operators' sites. A pixel establishes a tracking
account. Platform, tenant and owner say nothing about a template, because kits move across
platforms.

### 5.3 Combining the values of one capture pair

1. **Within a layer, only the best shared value counts.** The other shared values of that layer are
   listed in the ledger with 0 bits and the note "subsumed". Values in one layer are dependent
   (one kit brings many identical files), and averaging keeps the score honest.
2. **Across layers, the bits add.** Different actors set different layers (the kit vendor sets the
   kit, the platform sets the tenant, the media buyer sets the ad account), so the layers are treated
   as independent. This is an assumption; section 9 lists it.
3. **Conflicts.** A tenant or an owner is a single-valued id per page. When the two captures show
   different values of it under the operator or campaign relation, the pair gets a conflict row of
   **−4 bits** (`conflict_bits`) once for that layer, and the pair is a **cannot-link**: the verdict
   can never be `link`. A platform conflict costs bits only, since one operator can run several
   platforms. A conflict on the template or the ecosystem relation is not counted.
4. **Platform and creative gate (HACK-66).** Under the campaign, operator and tracking-account
   relations, a pair needs at least one positive row outside the platform and creative layers whose
   value sits below platform level. The level is the value's place in the discovered hierarchy, or
   else its kind's usual `Indicator.level`. Platform constants and platform-level values in other
   layers (third-party hosts, page and form shapes) are shared by every tenant. A creative on its own
   (an ad, a celebrity's face or photo) can be reused by anyone. Without such a row the pair counts
   at most one positive layer and no live private id, so it never links or assigns. Its bits stay in
   the ledger, and an assignment explains it under the ecosystem relation instead.
5. **Corroborated landing build (HACK-84, campaign relation).** A copied kit alone is template
   evidence: under the campaign relation the kit layer counts at half scale and does not open the
   campaign gate. One campaign's captures, however, usually share one landing build. When the pair
   shares a whole-page skeleton or a normalised inline script that at most 2 pre-groups carry, and
   something independent backs it (a second such rare structure kind, or a rare shared creative
   image or masked text template), the structure kinds count at full scale, count as campaign
   evidence, and the page structure and the page script take one kit-layer term each. A build shared
   by three or more pre-groups, or with nothing backing it, stays template-level. Constants:
   `weights.KIT_BUILD_*`; the check is `ledger.kit_build`.
6. **Missing is 0 bits, never negative.** If one capture has a layer and the other does not (for
   example, no HAR for one of them), the ledger shows a `missing` row with 0 bits. A missing modality
   is not a penalty. Section 6 uses it to say what an item could still add.

### 5.4 Worked example (invented values)

The values below are invented. The priors, scales and rules are the ones in the code, and the
rarity counts `k` are assumed for the example. Two captures are compared under the campaign
relation in a collection of 30 captures (the 2026-11-22 demo slice, a snapshot), where a campaign typically has 3 captures.

| Layer | Kind (prior × scale) | Shared value (invented) | k | Bits | Status |
|---|---|---|---:|---:|---|
| `tracking_account` | `meta_pixel` (3.0 × 1.0), live on both sides | `pixel-demo-1000001` | 1 | 3.0 | shared, strict |
| `tenant` | `aio_cdn_tenant` (6.0 × 1.0), single-valued, live on both sides | `tenant-demo-07` | 2 | 5.0 | shared |
| `landing` | `aio_landing` (4.0 × 1.0) | `landing-demo-42` | 1 | 4.0 | shared |
| `kit` | `kit_asset_set` (6.5 × 0.8 = 5.2) | `set-demo-a1` | 4 | 3.2 | shared |
| `creative` | `ad_caption` (2.0 × 1.0) | `caption-demo-x` | 3 | 0 | hub-subsumed (0.415 < 0.5) |
| `collector` | `viewport` | `1920x1080` | — | 0 | collector-excluded |

Score: 3.0 + 5.0 + 4.0 + 3.2 = **15.2 bits**. Four layers are positive and the pixel is a live
operator-private id, so the edge qualifies. The threshold for this relation is τ = log2(30/3) + 3 =
6.3 bits (section 6), and the assignment bar with 17 campaigns is τ + log2 17 = 10.4 bits. The pair
clears the bar by 4.8 bits.

If the item had no HAR, the tracking layer would be missing, the score would fall by 3.0 bits to
12.2 (still above the bar), and the ledger would say "only on one side; other side lacks the
tracking_account layer" with 0 bits. The missing layer costs the item nothing.

### 5.5 Chain rule and explain-away (on by default)

The ids of one operator form a chain: platform ⊃ tenant ⊃ owner ⊃ aio campaign ⊃ landing. A value
that is shared by many owners is a platform constant, and a value that names one owner sits at the
owner level. The level of each value is discovered from the data (`ftf.linking.hierarchy`), so the
ledger does not need a hand-written level for every kind.

- **One term per level.** A shared fine id counts the level it names and the coarser levels its
  parent ids determine. Values at the same level count once, however many of them match (the best
  one counts). The platform constants together count as one platform term, capped at the platform
  layer's weight.
- **Explain-away.** If two captures share the coarser ids (the same tenant or owner) but differ on a
  chain id (another campaign of the same owner), the shared coarse ids no longer count for the
  campaign relation: "same owner, other campaign" predicts them as well as "same campaign" does. Those
  rows become `hub_subsumed`, with 0 bits, and the conflict row explains why.

The worked example in 5.4 is computed with the per-layer rule that sections 5.1 to 5.3 describe. With
the chain rule on, the tenant and landing terms are set by the discovered levels, so for a real pair
read the ledger rows that `ftf score` or `ftf export` writes rather than recomputing the example.

### 5.6 Calibrated content views (off by default)

Text embeddings, image embeddings, perceptual hashes and MinHash signatures are continuous: two
creatives of one campaign are *similar*, not equal. `ftf.linking.similarity` turns one such
similarity into bits with a calibration fitted per view, and the ledger adds it as one row.

- **Calibration.** `ftf calibrate-similarity DATASET -o calibration.json` groups the dataset with the
  ledger itself (identifiers only, so a view never labels its own training pairs). Pairs of captures
  in different campaign groups *and* different operator groups form the null; pairs in one campaign
  group are the positives. Two campaigns of one operator may reuse a creative, so they belong to
  neither side. With at least 10 positives the map is a class-balanced logistic regression; with
  fewer it is the tail-LR bound of the null (how rare the similarity is among unrelated pairs). The
  map is monotone, never negative (a dissimilar creative is no evidence against a link, campaigns
  swap creatives) and capped at 4 bits. The quality report gives AUC, Cllr, Cllr_min and the
  similarity that earns 1, 2 and 4 bits for each view.
- **In the ledger.** With `--content-views calibration.json` (`ftf score`, `ftf link`, `ftf assign`)
  each view adds a row in its indicator's own layer, scaled by that layer's relation scale (halved
  again under operator), so the within-layer max holds it against the exact-match kinds of the
  layer: a similar image and an equal image hash count once. A face match only says "same
  celebrity", so a face view is capped at 1 bit unless its null was drawn from other scams with the
  same face.
- **Bits, not layers.** A content row raises the score but never makes a layer positive, never opens
  the campaign gate and is never a live private id. Content similarity alone can therefore lift a
  screenshot-only item to review, with the similar creatives named in the candidate notes, but it
  never makes a link or an assignment.
- **Candidate search.** The candidate index proposes campaigns through shared exact values, and a
  similar creative shares none, so with content views on `ftf assign` scores every campaign (the
  linear scan the index is tested against). Off, nothing changes: the scores are the same as
  without the switch.

## 6. Relations and grouping

One capture pair can be the same campaign under one relation and merely the same template under
another, so the system keeps one partition **per relation**. They are not nested, because one landing
can run under two pixels and one pixel can serve two landings.

| Relation | Typical size m | Layers needed for an edge |
|---|---:|---:|
| same template | 8 | 1 |
| same tracking account | 4 | 2 |
| same operator | 6 | 2 |
| same campaign | 3 | 2 |
| same ecosystem | 12 | 1 |

- **Threshold.** τ = log2(n / m) + 3 bits, where n is the number of canonical captures in the
  collection and m is the typical size above. The log term is floored at 0, so τ is never below 3
  bits. The bound behind it: if the scores were calibrated likelihood ratios, the chance that a
  false pair reaches τ bits is at most 2^−τ (Markov's inequality, e-values). The 3 bits are a safety
  margin.
- **Edge.** A pair is an edge when its score is at least τ and it has enough positive layers for
  the relation (or a live operator-private id). Otherwise it is "review" when it is within 3 bits
  below τ, and "no link" below that.
- **Clustering.** Each relation is clustered by greedy average-linkage on signed weights, which is a
  correlation clustering in the GAEC style. An edge contributes (score − τ), a non-edge contributes
  min(score − τ, 0), and a pair that shares nothing contributes −τ. Two clusters merge when their
  mean cross weight is non-negative and at least one cross pair is an edge, and never when a
  cannot-link pair sits between them. Unlike connected components, one false private-id match cannot
  join two campaigns, because the other cross pairs outvote it.
- **Output.** Each relation gives its campaigns, and each campaign keeps its links, so the report can
  show which captures are held together by which evidence.

## 7. Assign, new or review

For a new item, `ftf assign` scores the item against the campaigns of the relation. Its probe
captures are its non-decoy captures (decoys only if nothing else exists). For each campaign it takes
the best pairwise score between a probe and a campaign member. With K campaigns of the relation:

- **assign** to the best campaign when its score is at least τ + log2 K (the bar), the best pair
  qualifies as an edge, no cannot-link conflict exists with a member of that campaign, and it leads
  every other campaign that could be assigned the same way by at least 1 bit (a likelihood ratio
  of 2). A smaller lead is a tie at the top: the item goes to review with both campaigns named.
  The margin is the same for every relation;
- **new** when the best score is at most τ − 3, the item's evidence capacity is at least τ, **and**
  the best score plus the capacity of its missing modalities stays below the bar;
- **review** otherwise, including every case where the item could still earn enough evidence to
  reach τ, or where its missing inputs could lift the best score to the bar. An item with a
  conflict on a campaign member goes to review, not assign.

**Evidence capacity** is the most bits the item could still earn: for each layer it can show, the
largest weight the table allows to a kind the item's modalities can carry, summed over the layers
the item's modalities can reach. Which kinds need which modality is a small explicit table next to
the weights (`weights.KIND_MODALITIES`: asset-file hashes need saved assets, recorded traffic or an
MHTML; redirect hops need recorded traffic or the fetcher's redirect log; unlisted kinds come from
any modality). A private id without a live modality (recorded traffic or the fetcher's redirect
log) counts its weight minus the copy discount, as the ledger scores it. A
screenshot-only item can show a creative and nothing else, so its capacity is far below τ. It goes to
review, never to new. The item is not evidence that it is a new campaign; it is evidence that the
pages that would settle the question are missing. Each assignment lists the modalities whose absence
limits it.

The **capacity of the missing modalities** is how much the capacity would rise if every absent
modality were present: new layers they open, and higher caps in layers the item already shows (a
HAR makes private ids live and adds redirect hops), each layer counted once. If the best candidate's score plus that capacity reaches the bar, the missing input
could have produced the assignment, so a low score is not evidence of a new campaign: the item goes
to review and the note names the missing inputs. Both capacities are upper bounds from the weight
table; they never add bits to a score.

## 8. Explanation

Every link carries its **ledger**: one row per layer and kind, with the value (truncated), the
status (`shared`, `conflict`, `missing`, `hub_subsumed`, `collector_excluded`), the bits, the number
of groups that carry the value and the indicator ids that support the row, so each row can be traced
back to the capture. The same ledger drives the demo app and the export (`evidence.csv`).

Links are labelled with the evidence tiers that NASK analysts already use (CERT Polska's reports
speak of Strong, Medium, Supplementary and insufficient evidence on their own):

| Tier | Our link level | Rule in `ftf.linking.ledger.level_of` |
|---|---|---|
| Strong | `strict` | a live operator-private id, or three or more positive layers |
| Medium | `medium` | two positive layers |
| Supplementary | `loose` | one positive layer; shared, but not enough alone |
| Insufficient alone | below the threshold | the score is under τ, so it does not stand alone |

A tenant or owner conflict caps a pair at Medium, because the pair is a cannot-link. The demo app
still labels "insufficient alone" with an adapter threshold (log2 of the number of captures) until
the engine publishes its τ in the dataset. The two differ, so read the score as well as the label.

## 9. Scalability and limits

**Scalability (design estimate, not benchmarked).** Candidate pairs come from an inverted index on
linkable values, so a pair is scored only when it shares at least one non-hub value, and values
carried by more than 500 captures are skipped. The ledger costs O(pairs × layers). The clustering is
greedy merging, O(E log E) for E edges. A new item is assigned with one index lookup per value and one
score per campaign member. The 2026-11-22 demo slice has 30 captures, so none of these numbers has been measured
at scale. The design targets 10^5 incidents; that target is a plan, not a result.

**Limits, stated plainly:**

- **The numbers are priors.** The structure (rarity over groups, the best value per layer, bits
  added across layers, collector exclusion, missing as 0) is the method. The weights and thresholds
  are explicit, auditable guesses, set from the cross-case reading and not fitted. A learned table
  (HACK-23) exists as an option (`--weights`), but the default run keeps the hand table. A ±2-bit sensitivity sweep is
  part of the evaluation protocol and is not yet run; links that flip under it will be flagged.
- **No true negatives in the case sample.** Every case is a scam, so every cross-campaign pair is a
  hard negative from a correlated population. The background corpus that would give real rarity
  estimates is not built.
- **Independence across layers is assumed.** Bits add across layers. Where two layers move together
  (one operator's pixel spanning two campaigns) the sum overstates the evidence, which is why the
  operator-level layers are scaled down under the campaign relation.
- **Content similarity is off by default.** Calibrated views (5.6) need Visual's vectors and a
  calibration fitted on the same kind of data; their positives come from the ledger's own campaign
  groups, so a campaign the identifiers miss teaches the calibration nothing.
- **Enrichment is best effort.** A registry, DNS or certificate lookup can fail. A failed lookup
  is recorded as `unknown` and carries no bits; a stale cache entry is labelled as stale.
- **Campaigns rotate.** Operators change pages and pixels inside a campaign. The campaign relation
  is strict for that reason, and screenshot-only variants go to review rather than to a campaign.
- **The evaluation is a regression check, not a benchmark.** The final report's raw counts are in the
  README ("Evaluation results"). Its protocols are time split (E7), hidden-layer recovery (E1),
  leave-one-out (E2), degraded inputs (E3), distractors (E4) and a ±2-bit sensitivity sweep (E6). With
  platform ids hidden the engine recovers 0 of 11 true campaign pairs and links none, so recall there is
  0 of 11 while precision is safe. On the distractors it passes all natural must-not-link pairs, brand
  negatives, platform-only and celebrity-only pages, but one of three synthetic kit copies still fails.
  No accuracy figure is claimed here.
