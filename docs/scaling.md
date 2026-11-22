# Scaling and sensitivity of the linking engine

How the engine behaves as the collection grows, and how much its decisions depend on the
hand-set weights. All numbers were measured on a shared cloud VM (Python 3.11) with `scripts/bench_linking.py`. Synthetic corpora contain only
invented values.

## What happens to a new capture

1. **Build once.** `ftf link` groups the collection into one partition per relation. `ftf index`
   then builds the candidate index (`src/ftf/linking/index.py`) and saves it as JSON. The index has:
   - an inverted index (layer, kind, normalised value) → captures. It holds informative values
     only: collector kinds, failed lookups and kinds weighted 0 bits under every relation are
     left out;
   - capture → campaign for each relation;
   - rarity counts for each value.
2. **Assign without re-clustering.** A new incident can earn positive bits only against a capture
   that shares one of its informative values. The engine therefore scores only the campaigns it
   can reach through those values:
   - values that would score below the hub floor after rarity are skipped;
   - the ledger's scoring context is extended by the incident (copy-on-write) rather than rebuilt;
   - the partition is never recomputed, and the assign / new / review decision is the same as a
     full scan over all campaigns. Tests check this on random synthetic corpora for every relation.
3. **Vector views (hook).** Each view (e.g. an ad-image embedding) stores a matrix, and the
   nearest neighbours are an exact top-k by dot product. NumPy alone handles this at our scale.

Complexity, with N captures, K campaigns, a incident captures and v values per capture:

| Step | Cost |
|---|---|
| `ftf link` | one ledger call per candidate pair (captures sharing a value), then greedy average linkage |
| index build | O(N · v), one pass over the indicators |
| assign with the index | O(a · v) lookups, then ledger calls only against the reachable campaigns' members; small O(N) parts remain (see below) |
| assign without the index | rebuilds the context for base plus incident (O(N · v)) and scores a × every campaign member |

## Measured timings

Re-measured after a later change (chain rule on, the default) on the organiser dataset rebuilt from main
7a6cd07 (25 incidents, 31 captures, 3,917 indicators). The shared VM was busy (load average about
10 on 4 cores): the full scan took 2.6× longer than in the first measurement of the same corpus
(65.5 s against 25.5 s at 10k), so compare the columns within a row, not across runs.

| Collection | Captures | Link | Index build | Assign, index (median) | Assign, full scan (median) | Same result |
|---|---:|---:|---:|---:|---:|---|
| Organiser dataset (25 incidents) | 31 | 2.0 s (all 5 relations) | 0.21 s | 72 ms | 515 ms | 5 / 5 |
| Synthetic | 1,000 | 6.6 s ¹ | 0.22 s | 155 ms | 1.3 s | 3 / 3 |
| Synthetic | 9,865 | 77 s ¹ | 4.0 s | 1.2 s | 65.5 s | 3 / 3 |

¹ Campaign relation only, linked one synthetic operator block at a time (`--blockwise`): the
blocks share nothing except a 0-bit platform constant. The index and assign timings do not
depend on how the partition was made.

At about 10k captures the index makes assignment about 55× faster. A single new capture is
assigned in about a second on the busy VM, against over a minute for the full scan.

The indexed cost still grows with N, for three reasons:
- The chain rule's id hierarchy (`hierarchy.py`) is a property of the whole collection: a value's
  level depends on all its carriers, and the level graph on every anchor id. The extended context
  rediscovers it over base plus incident, one pass over every capture's values. This is most of the
  indexed time at 10k (before that change the same assignment took 284 ms on a quiet VM). The result
  is memoised per incident, so `ftf assign --sensitivity` pays it once per incident, not per re-run.
  An incremental update (only the values and anchor ids the incident touches) is the next step
  if this matters.
- Pre-groups (rarity is counted over operators, not incidents) are recomputed when the new
  incident carries a live private or owner id.
- When fewer than five campaigns score above 0 bits, the candidate list is filled with
  zero-bit campaigns in id order, exactly as the full scan does. A cheap conflict check makes
  each step of that walk a few dictionary lookups.

### Scoring options

The index is built with the scoring options of `ScoringContext.from_dataset`: the weight table
(`--weights` from `ftf learn-weights`), its background counts and the chain rule. `ftf index` and
`ftf assign` take `--weights`; a saved index records a fingerprint of all three and refuses to
load under other options. The candidate bound uses the ledger's own rarity term (with background
counts it is below log2 of the pre-groups, so the bound is higher). A table passed to
`link_incident(weights=...)` that gives bits to a kind the index kept no postings for falls back
to the full scan.

### A known limit of `ftf link`: mid-size hubs

On a synthetic corpus of 1,000 captures linked in one piece, `ftf link` took **285 s**. One
platform constant sat on 476 landers. That is below the 500-posting hub cap
(`grouping.HUB_POSTINGS`), so it proposed all ~113k pairs of those landers to the ledger, and
each pair scored 0 bits. Above 500 postings a value stops proposing pairs.

The fix is to let the ledger's weights and rarity decide which values may propose pairs. It
changes grouping slightly: a hub-only pair with a platform conflict is no longer scored. It is
left as a follow-up. On the organiser sample, linking takes under a second.

## Sensitivity of the weights (E6)

`ftf sensitivity LINKED.json -o report.json [--annotate OUT.json]` recomputes every link. It
moves one layer's weights at a time by −2 and +2 bits (every kind in the layer and the layer
default, floored at 0), then all layers together. τ and rarity stay fixed. The verdict follows
the grouping edge rule: `link`, `review` (within 3 bits below τ), or `no link`.
- A link whose verdict flips under a single-layer move is **fragile**. The note goes on its
  ledger rows, e.g. "fragile: flips to review if kit −2 bits".
- `ftf assign --sensitivity` does the same for decisions. The note goes on the assignment and
  its candidate.

On the organiser dataset (rebuilt from main 7a6cd07, hand table, chain rule on; the base is all but
the last 5 incidents):

| Relation | Links | Fragile (one layer ±2 bits) | Flip only if every layer moves ±2 bits |
|---|---:|---:|---:|
| campaign | 9 | 0 | 1 |
| operator | 17 | 0 | 1 |
| tracking account | 0 | – | – |
| ecosystem | 20 | 2 (tenant −2) | 4 |
| template | 19 | 2 (kit −2, tenant −2) | 13 |

Campaign and operator links rest on several independent layers, so no single 2-bit change of one
prior undoes them. They have fewer links than in the first measurement (9 and 17, against 24 and
24); both the dataset (rebuilt indicators) and the scorer (chain rule, one term per level of the
operator's id chain) changed since, so this run does not separate the two causes. None is fragile. Template and ecosystem links rest on one or two layers (kit, tenant), so they are the
ones to check by hand.
