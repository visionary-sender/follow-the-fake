# Linking decisions

Short records of why the Linking engine decides the way it does on cases where a reader might
expect another answer. Each record gives counts and folder labels only, never the indicator values.
The terms are the ones in [methodology.md](methodology.md).

## Why "review" is the right answer when the platform layer is hidden (HACK-58)

**The case.** In the E7 time split (`ftf eval timesplit --engine linking`) the reference is the 15
BACKUP incidents. The three later items do_udostepnienia:1, :2 and :3 belong, by ground truth, to
the campaign of BACKUP:p1 / p7 / p15. With platform ids hidden, the engine sends :1 and :2 to
review and calls :3 new. It makes no false accepts (0 of 7 "new" items wrongly assigned). With
platform ids visible, :1 and :2 are assigned correctly.

**What is left after hiding.** Hiding removes 12 platform-id indicators from :1 and from :2, in
the owner, tenant, landing, kit and distribution layers. Those include the only campaign-level
ids the items carry (the platform's campaign and landing ids). What remains is the kit, platform
software, landing-shape, offer and infrastructure layers, one operator-level tracking pixel, and
for :2 a tracker instance. Item :3 has no HAR, so it never carried platform ids; its tracker id
sits only in static files.

**The ledgers.** The assign bar is τ + log2 K = 5.4 + log2 15 ≈ 9.3 bits (9.4 for :2), and an
item is called new at ≤ τ − 3 ≈ 2.4 bits.

| Later item | Best campaign-A pair | Positive layers after the campaign gate | Engine's top candidate | Decision |
| -- | -- | -- | -- | -- |
| :1 | 4.95 bits vs BACKUP:p15 (tracking pixel +3.0, kit +1.25, platform software +0.7) | 1 | campaign A, 4.95 bits | review |
| :2 | 4.95 bits vs BACKUP:p15 (same rows as :1) | 1 | BACKUP:p5, another campaign, 7.7 bits | review |
| :3 | −4.0 bits vs BACKUP:p1, p7 and p15 (platform software name differs) | 0 | BACKUP:p5, another campaign, 2.2 bits | new |

**Why no change gets these assigned without a false accept.**

- *Nothing campaign-level is left.* The only shared private value with campaign A is a tracking
  pixel, and a pixel is an operator-level id: one operator runs many campaigns with the same
  pixel (07 §2.2). The HACK-53 campaign gate caps such a pair at one positive layer for this
  reason. Lifting the gate for a pixel would undo HACK-53 and bring back the wrong merges
  HACK-53 to HACK-56 fixed.
- *There are no ad-level ids to count.* The only distribution-layer indicator in :1 and :2 is a
  platform id, and it is hidden. Neither item has an ad-level id left.
- *A missing-layer correction does not apply.* The evidence capacity of all three items is
  32.4 bits, far above the bar, so their missing inputs do not limit them. 07 §1.2 and §2.4 treat
  a missing modality as 0 bits plus a capacity note, never as a lower bar. Even without the gate
  and without the log2 K term, 4.95 bits is below τ.
- *Lowering the bar would assign to the wrong campaign.* For :2 the strongest candidate is
  BACKUP:p5 at 7.7 bits, which is not in its ground-truth campaign. Any relaxation that let :2
  pass would assign it there first.
- *Item :3 has no evidence for campaign A.* It shares no positive layer with any member, and its
  platform software name differs. Without the HAR that holds its platform ids, the engine cannot
  know it belongs to campaign A.

**The answer.** When the platform layer is hidden, the evidence for these items is
operator-level: it shows who runs the page, not which campaign it belongs to. "Review" is the
honest output. The engine shows the analyst the candidate campaigns, the ledger and the reason
("best 5.0 bits vs assign bar 9.3"). An analyst who adds the missing campaign-level evidence (the
HAR, or a confirmed ad) can then confirm the assignment. Assigning on an operator id alone is how
the wrong merges in HACK-53 to HACK-56 happened.
