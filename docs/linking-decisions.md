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
