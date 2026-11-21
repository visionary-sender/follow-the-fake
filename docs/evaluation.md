# Evaluation harness (`ftf eval`)

Goal: honest numbers that the linking works and does not over-link. The harness does not depend on
any engine. It scores whatever implements the engine shape below against ground truth that comes
from the scammers' own platform ids and from written analyst verdicts. Every metric is reported
with raw counts ("9 of 11 pairs"), never as one accuracy number. With about 25 cases this is a
regression suite, not a benchmark.

Organiser data and everything derived from it (datasets, labels, results) stay out of git. They
live on the workbench under `/data/derived/evaluation/` and in
`gs://follow-the-fake-data/outputs/evaluation/`.

## Units (E0)

The unit is a **capture**, not a case folder. Ids follow Ingestion's scheme (`ftf.ingest.ids`):

- an incident is `<source>:<case folder>`;
- a capture is `<incident>:<key>`, where the key is the capture's anchor in the folder, or `c1`
  when the whole folder is one capture.

Folder 2 holds two sites, so it gives two captures. Case 61 gives an ad, two landers and a decoy.
An exact duplicate keeps its id and points `duplicate_of` at the smallest identical id; metrics
and labels use canonical captures only. `ftf.evaluate.units.node_to_capture` is the only place
that maps the planning prototype's node names to capture ids.

## Dataset under evaluation

`--dataset FILE` takes any contract `Dataset`, for example Ingestion's output. Without it the
harness converts the planning prototype signals (`ftf eval prototype`, `ftf.evaluate.prototype`).
That conversion is a stopgap: switching to Ingestion's output is a path change. The kind → layer
and level table is `KIND_TABLE` / `CARD_TABLE` in `prototype.py`. Collector kinds (capture day and
time, screenshot date, saved-from path, visit ids) go to the collector layer.

## Ground truth: `labels.json`

`ftf eval labels` writes `<out>/labels/labels.json`:

| field | meaning |
|---|---|
| `schema` | `ftf-eval-labels@0.1` |
| `dataset` | name of the dataset the labels were derived from |
| `captures` | capture id → `{source, folder, key, canonical}`. The source key re-keys labels mechanically if capture ids change. |
| `groups[]` | `{id, level, relation, source, members[], member_keys[], reason, platform_values?}` |
| `must_not_link[]` | `{a, b, relation, source, reason}`: pairs that must not share a cluster of that relation |
| `summary` | counts per level, the number of must-not-link pairs, unresolved names, and `e1_discrepancies` |

The groups at each `level` form a partition of that level's universe, which is the captures the
level covers. `source` is one of:

- `platform_id`: captures that share a scammer platform id fall into one group (connected
  components). Levels and the engine relation they are compared with:
  - `campaign` (campaign id) → campaign;
  - `owner` (account id) → operator;
  - `tenant` (landing-cdn tenant) → operator;
  - `landing` (landing id) → campaign;
  - `template` (lander template id) → template.
- `analyst_verdict`: groups written down from the cross-case analysis, at the levels
  `analyst_campaign`, `analyst_operator` and `analyst_template`. Must-not-link pairs (E4a) carry
  the relation they forbid.

Every label has a human-readable `reason`. The derived platform-id groups are compared with the
groups the analysis expected. Any mismatch is printed and stored in `summary.e1_discrepancies`;
nothing is forced to match.

## Hidden layer and leak check (E1)

`hide_platform_ids(ds)` does three things. Platform constants (SDK assets, the platform's own
file account, API endpoints) stay, because they name the platform, not the tenant.

1. It removes every platform-id indicator: owner, campaign, tenant, landing, template, flow and
   handle key.
2. It drops asset URLs that embed a landing-cdn path. Content hashes stay.
3. It masks any other occurrence of a hidden value as `{hidden}`, for example the handle-key
   parameter name inside a URL parameter shape.

`leak_check(hidden, values)` searches every hidden value in every remaining indicator value. The
search is case-insensitive and looks for the value as is, without dashes, and as a uuid's 8-hex
prefix. Values of 8 or more characters match as substrings; shorter ones match only as whole
tokens. `ftf eval leak` writes `<out>/leak/leak.json` with the hit count (expected 0) and each
hit.

## Metrics (`ftf.evaluate.metrics`)

- `pairwise(pred, truth)`: P, R and F1 with `tp`, `predicted_pairs` and `true_pairs`.
- `bcubed(pred, truth)`: item-level B-cubed P and R.
- `macro_bcubed(pred, truth)`: B-cubed averaged within each label group first, then over the
  groups, so one big group does not dominate.
- `must_not_link(pred, pairs)`: how many forbidden pairs share a cluster.
- `openset_counts(outcomes)`: correct, wrong, new, review and false accepts.

Scoring is restricted to the label universe. A labelled capture that the prediction does not
place anywhere counts as a singleton.

## Engines (`ftf.evaluate.engine`)

The shape is Linking's API (HACK-21):

```python
engine.link_dataset(ds) -> Dataset          # links + campaigns per relation
engine.link_incident(base, incident, relation=Relation.campaign) -> IncidentResult  # .assignment, .links
get_engine("baseline" | "linking")
```

`baseline` is a naive linker: connected components over any shared non-collector value. It is
the "loose" floor that is expected to merge everything. `linking` wraps `ftf.linking` once that
package is installed.

## Commands

Defaults: `--out-dir /data/derived/evaluation` (or `$FTF_EVAL_DIR`) and `--prototype-dir
/data/derived/evaluation/prototype`.

| command | writes |
|---|---|
| `ftf eval prototype` | `<out>/prototype-contract/dataset.json` |
| `ftf eval labels [--dataset F]` | `<out>/labels/labels.json` |
| `ftf eval leak [--dataset F]` | `<out>/leak/leak.json` |
| `ftf eval baseline [--dataset F] [--engine baseline\|linking] [--no-hide]` | `<out>/baseline/<engine>.json`: per label level, pairwise and B-cubed counts and must-not-link violations |

A new subcommand is one `register(subparsers)` function appended to `SUBCOMMANDS` in
`ftf/evaluate/commands.py`. It uses `add_common` and `load_dataset` for the shared options.

Sync the results to the bucket from the VM:
`gcloud storage rsync -r /data/derived/evaluation/ gs://follow-the-fake-data/outputs/evaluation/`.
