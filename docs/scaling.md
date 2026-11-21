# Scaling and sensitivity of the linking engine

How the engine behaves as the collection grows, and how much its decisions depend on the
hand-set weights. All numbers were measured on the shared workbench VM (`ftf-workbench`, Python 3.11) with `scripts/bench_linking.py`. Synthetic corpora contain only
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

| Collection | Captures | Link | Index build | Assign, index (median) | Assign, full scan (median) | Same result |
|---|---:|---:|---:|---:|---:|---|
| Organiser sample (25 incidents) | 31 | 0.8 s (all 5 relations) | 0.12 s | 15 ms | 161 ms | 5 / 5 |
| Synthetic | 1,000 | 2.1 s ¹ | 0.05 s | 31 ms | 380 ms | 10 / 10 |
| Synthetic | 9,865 | 30 s ¹ | 1.3 s | 284 ms | 25.5 s | 10 / 10 |

¹ Campaign relation only, linked one synthetic operator block at a time (`--blockwise`): the
blocks share nothing except a 0-bit platform constant. The index and assign timings do not
depend on how the partition was made.

At about 10k captures the index makes assignment about 90× faster. A single new capture is
assigned in under 0.3 s, against 25 s for the full scan.

The indexed cost still grows with N, for two reasons:
- Pre-groups (rarity is counted over operators, not incidents) are recomputed when the new
  incident carries a live private or owner id.
- When fewer than five campaigns score above 0 bits, the candidate list is filled with
  zero-bit campaigns in id order, exactly as the full scan does. A cheap conflict check makes
  each step of that walk a few dictionary lookups.

### A known limit of `ftf link`: mid-size hubs

On a synthetic corpus of 1,000 captures linked in one piece, `ftf link` took **285 s**. One
platform constant sat on 476 landers. That is below the 500-posting hub cap
(`grouping.HUB_POSTINGS`), so it proposed all ~113k pairs of those landers to the ledger, and
each pair scored 0 bits. Above 500 postings a value stops proposing pairs.

The fix is to let the ledger's weights and rarity decide which values may propose pairs. It
changes grouping slightly: a hub-only pair with a platform conflict is no longer scored. It is
left as a follow-up. On the organiser sample, linking takes under a second.

## Sensitivity of the weights (07 §4, E6)

`ftf sensitivity LINKED.json -o report.json [--annotate OUT.json]` recomputes every link. It
moves one layer's weights at a time by −2 and +2 bits (every kind in the layer and the layer
default, floored at 0), then all layers together. τ and rarity stay fixed. The verdict follows
the grouping edge rule: `link`, `review` (within 3 bits below τ), or `no link`.
- A link whose verdict flips under a single-layer move is **fragile**. The note goes on its
  ledger rows, e.g. "fragile: flips to review if kit −2 bits".
- `ftf assign --sensitivity` does the same for decisions. The note goes on the assignment and
  its candidate.

On the organiser sample (25 incidents, with the hand table after HACK-42):

| Relation | Links | Fragile (one layer ±2 bits) | Flip only if every layer moves ±2 bits |
|---|---:|---:|---:|
| campaign | 24 | 0 | 2 |
| operator | 24 | 0 | 2 |
| tracking account | 0 | – | – |
| ecosystem | 26 | 0 | 3 |
| template | 26 | 3 | 16 |

Campaign and operator links rest on several independent layers, so no single 2-bit change of one
prior undoes them. Template links rest mostly on the kit layer alone, so they are the ones to
check by hand.
