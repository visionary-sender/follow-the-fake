# Evaluation harness (`ftf eval`)

Goal: honest numbers that the linking works and does not over-link. The harness does not depend on
any engine. It scores whatever implements the engine shape below against ground truth that comes
from the scammers' own platform ids and from written analyst verdicts. Every metric is reported
with raw counts ("P 9 of 11 pairs"), never as one accuracy number. With about 25 cases this is a
regression suite, not a benchmark.

Organiser data and everything derived from it (datasets, labels, results, the private spec) stay
out of git. They live on the workbench under `/data/derived/evaluation/` and in
`gs://follow-the-fake-data/outputs/evaluation/`. This document describes structure only; no
organiser values appear in the repository.

## Units (E0)

The unit is a **capture**, not a case folder. Ids follow Ingestion's scheme (`ftf.ingest.ids`):

- an incident is `<source>:<case folder>`;
- a capture is `<incident>:<key>`, where the key is the capture's anchor in the folder, or `c1`
  when the whole folder is one capture.

An exact duplicate keeps its id and points `duplicate_of` at its canonical capture. Labels and
metrics use canonical captures only. `ftf.evaluate.units` holds the helpers: `canonical(ds)`,
`incident_of(ds)` and `source_keys(ds)`, which gives each capture its `(source, folder, key)`.

## Dataset under evaluation

`--dataset FILE` takes any contract `Dataset`. The default is the promoted Ingestion build,
`/data/derived/ingest/organiser/dataset-indicators.json` (override with `$FTF_EVAL_DATASET`). Every
command refuses a dataset that has none of the ground-truth platform kinds (exit 2), so a
captures-only file cannot produce numbers by accident.

## Private spec: `ground-truth-spec/spec.json`

The analyst verdicts and the expected E1 groups name organiser captures, so they are data, not
code. The spec is read at run time from `--spec FILE`, else `$FTF_EVAL_SPEC`, else
`/data/derived/evaluation/ground-truth-spec/spec.json`. Sync it from
`gs://follow-the-fake-data/outputs/evaluation/ground-truth-spec/`.

Schema `ftf-eval-spec@0.2` (`ftf.evaluate.units.GroundTruthSpec`). Unknown top-level fields are an
error; `note` is allowed as free text.

| field | shape |
|---|---|
| `schema` | `"ftf-eval-spec@0.2"` (required) |
| `analyst_groups` | `{<level>: [{members: [capture id], relation: <Relation>, reason: str}]}`. The level name is free text, for example `analyst_campaign`. |
| `must_not_link` | `[{a: [capture id], b: [capture id] or ["*"], relation: <Relation>, reason: str}]`. `["*"]` means every other canonical capture. |
| `expected_e1` | `{<level>: [[capture id], ...]}`: the groups the E1 check expects the platform-id groups to match |
| `collapse_incidents` | `[incident id]`: captures of these incidents count as one unit in the E1 comparison |

`<Relation>` is one of `template`, `tracking_account`, `operator`, `campaign`, `ecosystem`. A
capture id named in the spec that is not in the dataset is reported, never silently dropped.

## Ground truth: `labels.json`

`ftf eval labels` writes `<out>/labels/labels.json`, schema `ftf-eval-labels@0.2`
(`ftf.evaluate.labels`):

| field | meaning |
|---|---|
| `schema` | `ftf-eval-labels@0.2` |
| `dataset` | name of the dataset the labels were derived from |
| `captures` | canonical and duplicate capture id → `{source, folder, key, canonical}`. The source key re-keys labels mechanically if capture ids change. |
| `groups[]` | `{id, level, relation, source, members[], member_keys[], reason, platform_values?}`. `member_keys` are the `(source, folder, key)` triples. `platform_values` is present on platform-id groups. |
| `must_not_link[]` | `{a, b, relation, source, reason}`: capture pairs that must not share a cluster of that relation |
| `summary` | `captures`, `canonical_captures`, `groups_per_level` (groups, non-singleton groups, captures), `platform_levels_without_indicators`, `must_not_link_pairs`, `missing_captures`, `e1_discrepancies` |

`source` is one of:

- `platform_id`: captures that share a scammer platform id form one group (connected components).
  Each level is compared with the engine relation shown:

  | level | platform indicator kind | relation |
  |---|---|---|
  | `campaign` | `aio_campaign` | campaign |
  | `owner` | `aio_owner` | operator |
  | `tenant` | `aio_cdn_tenant` | operator |
  | `landing` | `aio_landing` | campaign |
  | `template` | `aio_lander_template` | template |

- `analyst_verdict`: groups and must-not-link pairs from the spec, keyed by capture id.

The groups at each level form a partition of that level's universe, the captures the level covers.
Every label has a human-readable `reason`. The platform-id groups are compared with the spec's
`expected_e1` groups; every mismatch is printed and stored in `summary.e1_discrepancies`, and
nothing is forced to match.

## Hidden layer and leak check (E1)

`hide_platform_ids(ds)` returns `(hidden_ds, hidden_values)`. Platform constants (SDK assets, the
platform's own file account, API endpoints) stay, because they name the platform, not the tenant.

1. It removes every indicator whose kind is in `HIDDEN_KINDS` (`ftf.evaluate.leak`): owner,
   campaign, tenant, landing, lander template, flow, handle key and the related name and form kinds.
2. It drops asset URLs whose landing-cdn path segment is a hidden value. Content hashes stay.
3. It masks every other occurrence of a hidden value as `{hidden}`, in every string field.

`leak_check(hidden, values)` searches each hidden value in every remaining string. The search is
case-insensitive and matches the value as is, without dashes, and as a uuid's 8-hex prefix. Values of
8 or more characters match as substrings; shorter ones match only as whole tokens. `ftf eval leak`
writes `<out>/leak/leak.json` with the counts and each hit. The expected hit count is 0.

## Metrics (`ftf.evaluate.metrics`)

- `pairwise(pred, truth)`: `tp`, `predicted_pairs`, `true_pairs`, precision, recall, F1 and a text
  form such as `P 9 of 11 pairs, R 9 of 12 pairs`.
- `bcubed(pred, truth)`: item-level B-cubed precision and recall.
- `macro_bcubed(pred, truth)`: B-cubed averaged within each label group first, then over the groups.
- `must_not_link(pred, pairs)`: `pairs`, `violated` and the violating pairs.
- `openset_counts(outcomes)`: correct, wrong, new, review and false accepts.
- `score_against_labels(clustered, labels)`: every label level against the engine's partition of
  that level's relation.

Scoring is restricted to the label universe. A labelled capture that the prediction does not place
anywhere counts as a singleton.

## Engines (`ftf.evaluate.engine`)

Engines are called only through this shape, which is Linking's API (`ftf.linking`):

```python
engine.link_dataset(ds) -> Dataset                       # links + campaigns for every relation
engine.link_incident(base, incident, relation) -> IncidentResult   # .assignment, .links
get_engine("baseline" | "linking") -> Engine
```

- `baseline` (`BaselineEngine`): connected components over any shared linkable (non-collector)
  value, per relation. It is the loose floor and is expected to merge too much.
- `linking` (`LinkingEngine`): calls `ftf.linking.link_dataset(ds)` and
  `ftf.linking.link_incident(base, incident, relation=relation)`. Both are exported from
  `ftf.linking`. `link_incident` raises `ValueError` if the incident is already in `base`.

## Commands

Defaults: `--out-dir /data/derived/evaluation` (override with `$FTF_EVAL_DIR`), `--dataset` as above,
`--spec` as above. Every command exits 2 with a message on missing ground truth or a missing input.

| command | writes |
|---|---|
| `ftf eval labels [--dataset F] [--spec F]` | `<out>/labels/labels.json`; prints discrepancies and missing captures |
| `ftf eval leak [--dataset F]` | `<out>/leak/leak.json`: `hidden_indicators`, `remaining_indicators`, `hidden_values`, `masked_indicator_values`, `masked_strings`, `hits`, `hit_rows` |
| `ftf eval baseline [--dataset F] [--spec F] [--engine baseline\|linking] [--no-hide]` | `<out>/baseline/<engine>-<hidden\|visible>.json`: per label level, pairwise, B-cubed, macro and must-not-link counts |

`--no-hide` runs the engine on the visible dataset, which measures how much the platform ids help.

A new subcommand is one `register(subparsers)` function appended to `SUBCOMMANDS` in
`ftf/evaluate/commands.py`. It uses `add_common` and `load_dataset` for the shared options.

Run on the workbench (never on the laptop):

```bash
scripts/vm.sh run -- ftf eval labels
scripts/vm.sh run -- ftf eval leak
scripts/vm.sh run -- ftf eval baseline --engine baseline
scripts/vm.sh run -- ftf eval baseline --engine linking
```

Sync the results to the bucket from the VM:
`gcloud storage rsync -r /data/derived/evaluation/ gs://follow-the-fake-data/outputs/evaluation/`.
