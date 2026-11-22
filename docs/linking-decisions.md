# Linking decisions

Short records of why the Linking engine decides the way it does on cases where a reader might
expect another answer. Each record gives counts and folder labels only, never the indicator values.
The terms are the ones in [methodology.md](methodology.md).

## Hidden platform layer: review for two later items, one miss (HACK-58)

**The case.** In the E7 time split (`ftf eval timesplit --engine linking`), the reference is the
15 BACKUP incidents, which the engine groups into K = 15 reference campaigns. The 13 later items
are assigned one at a time. By ground truth, do_udostepnienia:1, :2 and :3 belong with BACKUP:p1,
p7 and p15. With platform ids hidden, the engine sends :1 and :2 to review and calls :3 new. That
:3 call is a miss. There are 0 false accepts among the 10 later items whose expected answer is new.
With platform ids visible, :1 and :2 are assigned, correctly under E7's scoring.

**What is left after hiding.** Hiding removes 12 platform-id indicators each from :1 and :2, in
the owner, tenant, landing, kit and distribution layers. Those include the only campaign-level
ids the items carry: the platform's campaign and landing ids. What remains is the kit, platform
software, landing shape, offer and infrastructure layers, one tracking pixel, and for :2 a tracker
instance. Item :3 has no HAR, so it never carried platform ids. Its tracker id sits only in static
files.

**The ledgers.** The assign bar is τ + log2 K. For :1 and :3 that is 5.4 + log2 15 ≈ 9.3 bits,
and the new cut is τ − 3 ≈ 2.4 bits. For :2 they are 9.4 and 2.5 bits. All three items have a
measured evidence capacity of 32.4 bits. The missing inputs therefore cap none of them: the
evidence they can show is simply not campaign-level.

| Later item | Best pair with a BACKUP:p1 / p7 / p15 campaign | Positive layers after the campaign gate | Engine's top candidate | Decision |
| -- | -- | -- | -- | -- |
| :1 | 4.95 bits vs BACKUP:p15 (tracking pixel +3.0, kit +1.25, platform software +0.7) | 1 | BACKUP:p15's campaign, 4.95 bits | review |
| :2 | 4.95 bits vs BACKUP:p15 (same rows as :1) | 1 | BACKUP:p5's campaign (not in its truth group), 7.7 bits | review |
| :3 | −4.0 bits vs BACKUP:p1, p7 and p15 (the platform software name differs) | 0 | BACKUP:p5's campaign (not in its truth group), 2.2 bits | new (miss) |

**Why :1 and :2 should stay in review.**

- *Nothing campaign-level is left.* The only private value they share with the target campaign
  is a tracking pixel. A pixel identifies an operator, and one operator runs several campaigns
  with the same pixel. The campaign gate therefore lets a pair count more than one positive layer
  only if it shares a campaign-level value: an ad or ad set, a landing or own domain, or a
  platform campaign, landing or offer id. Lifting the gate for a pixel would bring back the wrong
  merges between campaigns run by one operator that the gate was added to stop.
- *There are no ad-level ids to count.* The only distribution-layer indicator in :1 and :2 is a
  platform id, and it is hidden.
- *A missing-layer correction does not apply.* A missing modality adds 0 bits and a capacity
  note. It never lowers the bar, and capacity is not what limits these items. Even with no gate
  and no log2 K term, 4.95 bits is below τ.
- *Lowering the bar would pick the wrong campaign.* For :2 the strongest candidate is BACKUP:p5's
  campaign at 7.7 bits, and BACKUP:p5 is not in its ground-truth group. Any relaxation that let :2
  pass would assign it there.

The engine tells the analyst why. For :1 the note reads "best 5.0 bits vs assign bar 9.3". For :2
it reads "best 7.7 bits vs assign bar 9.4". Both notes list the candidate campaigns and their
ledgers. An analyst who adds campaign-level evidence, such as the HAR or a confirmed ad, can then
confirm the assignment.

**Why :3 is a miss, not a defensible answer.** Item :3 shares no positive layer with any member
of the target campaigns, and its platform software name conflicts with theirs. Its best score of
2.2 bits, against another campaign, falls at or below the new cut, so the engine calls it new.
The HAR that would carry its campaign-level ids is in another folder, so the engine cannot see
the evidence that it belongs with BACKUP:p1, p7 and p15. Under the campaign relation, a no-HAR
item arguably belongs in review rather than new. That change is outside this record.

## Missing inputs that could reach the bar: review, not new (HACK-67)

**The rule.** A `new` call becomes `review` when the best candidate's score plus the capacity of
the item's missing modalities reaches the assign bar. The missing input could have produced the
assignment, so a low score is no evidence of a new campaign. The note names the missing inputs and
the bits they could add. Both the linear scan and the indexed path apply the rule, with identical
results (tested).

**Capacity per kind.** Before this change capacity was counted per layer. In the challenge data,
every layer a HAR feeds is also fed by HTML somewhere, so a missing HAR never added anything:
do_udostepnienia:3 (HTML, screenshot and assets; HAR, video and ad library missing) had a capacity
of 32.4 bits, the same as a complete item, and its note said no missing input could add evidence.
Capacity now counts, per layer, only the kinds the item's modalities can carry
(`weights.KIND_MODALITIES`), and a private id without recorded traffic counts minus the copy
discount, as the ledger scores it. For :3 that gives 30.9 bits, and a missing HAR adds at most
1.5 bits (offer and tracker instance layers).

**Effect on the evaluations.** Item :3 stays `new`: 2.2 + 1.5 bits is far below the 9.3-bit bar.
Its campaign is out of reach for another reason: against BACKUP:p1, p7 and p15 it scores −4.0 bits,
because the platform software name differs, and a HAR cannot remove that conflict.

| Run | Before | After |
| -- | -- | -- |
| E7 time split, platform ids hidden | 0 correct, 6 review, 7 new (6 correct), 0 false accepts | same |
| E7 time split, platform ids visible | 2 correct, 4 review, 7 new (6 correct), 0 false accepts | same |
| E1 hidden platform layer | 0 wrong merges, 0 must-not-link violations | same |
| E3 degrade, false accepts | 0 of 108 items | 0 of 108 items |
| E3 degrade, HTML-only median capacity | 48.4 bits | 44.9 bits |

**What the bound does not see.** Per kind, HTML can carry almost everything a HAR can, so the bound
credits a missing HAR with little more than the live-id discount. In the degrade run, items with a
HAR are assigned (leave-one-item-out: 7 of 8 with all inputs, 5 of 8 with the HAR only) while the
same items with HTML only are not (5 new, 3 review), because their saved HTML lacks the ids the
recorded traffic shows. A bound that starts from the indicators the item actually carries (a layer
it shows nothing in gets the missing modality's full cap) would send those items and :3 to review.
That changes what capacity means for the capacity-below-τ rule too, so it is left for a separate
decision.

## Brand skin, platform constants and a creative alone never link into a campaign (HACK-66)

**The case.** E4 (`ftf eval distractors --engine linking`) adds pages that must stay outside every
campaign. On the organiser dataset, the engine linked 34 of 58 real outlet pages (e), 3 of 3
platform-only pages (c) and 2 of 2 creative-only incidents (f). The engine called every one of those
items `new`, but `link_incident` still returned a loose campaign link for any candidate pair with
positive bits, and E4 counts any campaign link as a violation.

**What carried the links.** For (e) it was one shared third-party domain: the outlet's own CDN or
analytics host, which a scam clone also loads. For (c) it was the platform SDK asset, sometimes with a
third-party host. For (f) it was the ad id of the copied creative. Ingestion files third-party hosts
and domains at platform level.

**The rules.**
- *Platform and creative gate.* Under the campaign, operator and tracking-account relations, a pair
  needs one positive row outside the platform and creative layers whose value sits below platform
  level. Without one, the pair counts at most one positive layer and no live private id, so it never
  links or assigns. `link_incident` then explains the pair under the ecosystem relation.
- *Brand skin.* `ftf brand-stoplist` hashes every value on the corpus's real outlet pages. The default
  path loads it (`$FTF_BRAND_STOPLIST`, else a default path on the workbench). A stop-listed value is
  `hub_subsumed` with 0 bits under every relation except ecosystem. The `brand_asset` kind counts under
  ecosystem only. Without the file the engine warns and scores as before.

**Not circular.** The stop-list is built from the same real pages that E4 (e) uses. With the stop-list
switched off (`FTF_BRAND_STOPLIST=off`), (e) is still 0 of 58, so the gate
alone carries the result. The stop-list matters for kinds the gate does not cover, for example an
outlet's analytics id below platform level.

| Check | Before | After |
| -- | -- | -- |
| E4 (c) platform-only | 3 of 3 fail | 0 of 3 |
| E4 (d) collector injection | 0 of 2 | 0 of 2 |
| E4 (e) brand negatives | 34 of 58 fail | 0 of 58 (also 0 without the stop-list) |
| E4 (f) creative-only | 2 of 2 fail | 0 of 2 |
| E4 (b) kit copy | 1 of 3 fail | 1 of 3 (outside this change) |
| E1 hidden, E7 time split (hidden and visible), E3 degrade | | identical output |
| `ftf eval baseline --no-hide` (visible platform ids) | | identical output |
| `ftf eval baseline` (platform ids hidden), operator vs owner | pairwise 7/7 of 23 | 6/6 of 23, precision still 1.00 |

**The one lost pair.** With platform ids hidden, the two pages of do_udostepnienia:61 were linked
under the operator relation at 5.75 bits against a 5.46-bit bar. They share a live ads id, an inline
script, a form field set and third-party domains. The best of those domains is one that real outlet
pages also load, so the stop-list takes it to 0 bits. The next domain is worth 0.5 bits, the pair
drops to 4.75 bits, and the verdict goes from link to review. That is the intended behaviour: a host
that real outlets load says nothing about the operator. With platform ids visible the pair is
unchanged.
