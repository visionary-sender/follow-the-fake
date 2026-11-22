# Data contract v0 (`0.1.0`)

The contract is the shared shape that extraction, linking, evaluation and the demo app exchange.
Code: `src/ftf/contract/` (pydantic v2). Every document is validated on load. The JSON Schema
comes from `ftf.contract.json_schema()`.

## Core idea

The **unit of analysis is a capture**: one visit or one saved item (an ad screenshot, a
pre-lander, a lander, a decoy page). It is not the case folder. One folder can mix two sites,
and one case can hold several captures. An **incident** is one submitted report and groups its
captures. Links and campaigns point at **capture ids**.

## Entities

| Entity | Key fields | Notes |
|---|---|---|
| `Dataset` | `contract_version`, `name`, `incidents[]`, `links[]`, `campaigns[]`, `assignments[]` | The top-level serialised document. Extraction output has incidents only. |
| `Incident` | `id`, `source`, `source_ref`, `captures[]` (≥1), `indicators[]` | One submitted item, self-contained. |
| `Capture` | `id`, `stage` (ad / pre_lander / lander / decoy / unknown), `observed`, `captured_on`, `artefacts[]`, `missing[]`, `duplicate_of` | `missing` lists modalities that are absent, with a reason. Exact duplicates point to the capture they collapse onto. |
| `Artefact` | `id`, `type` (ad_screenshot, screenshot, html, har, mhtml, video, url, text, asset, ad_library, other), `path`, `sha256`, `size` | A file or item that belongs to a capture. |
| `Indicator` | `id`, `capture_id`, `layer`, `kind`, `level`, `value` **or** `vector_ref`, `observed` (live / static), `provenance`, `extractor` | One extracted feature. |
| `VectorRef` | `store`, `key`, `model`, `dim` | An embedding stored outside the document. Documents never contain inline floats. |
| `Link` | `id`, `a`, `b` (capture ids), `relation`, `level`, `score_bits`, `method`, `evidence[]` (≥1) | A typed relation between two captures. |
| `EvidenceRow` | `layer`, `kind`, `status`, `value`, `bits`, `seen_in`, `indicator_ids[]`, `note` | One itemised row of a link's ledger. |
| `Campaign` | `id`, `label`, `relation`, `capture_ids[]`, `link_ids[]` | A cluster. |
| `Assignment` | `incident_id`, `candidates[]` (campaign_id, score_bits), `decision` (assign / new / review), `campaign_id`, `evidence_capacity_bits`, `method` | An open-set decision for a new item. |

Enums:

- **`layer`**: what aspect the indicator describes. creative, distribution_account,
  tracking_account, tracker_instance, kit, platform, tenant, owner, landing, offer,
  infrastructure, collector.
- **`level`**: the granularity of the entity a value identifies, from coarse to fine.
  ecosystem, platform, tenant, operator, campaign, landing, capture. A kit file can sit in the
  `kit` layer and still be platform-level.
- **`observed`**: `live` means seen in recorded network traffic (HAR or a sandbox fetch).
  `static` means seen in saved files (HTML, screenshot, metadata).
- **`relation`**: template, tracking_account, operator, campaign, ecosystem.
- **link `level`**: strict, medium, loose. These are the evidence tiers from the cross-case
  analysis.
- **evidence `status`**: shared, conflict, missing, hub_subsumed, collector_excluded.

## Invariants (enforced in code)

1. An indicator has exactly one of `value` or `vector_ref`.
2. **Collector exclusion is decided by type, not by frequency.** A collector field describes the
   analyst or the capture tool, not the scam. Examples: fingerprint, analyst IP, UA, viewport,
   fbclid session, local path, OS user name, capture time or day, screenshot date. The list is
   `COLLECTOR_KINDS`, and indicators of those kinds must use `layer = collector`.
   `linkable(indicator)` is false for the collector layer. An evidence row from the collector
   layer must have status `collector_excluded` and 0 bits. A dataset whose link uses a collector
   indicator as any other status is rejected.
3. `missing` and `hub_subsumed` rows carry 0 bits. A missing modality is never a penalty.
4. A capture cannot list a modality as both present and missing.
5. All references resolve: indicator → capture and artefact of the same incident, link → captures,
   evidence → indicators (same layer and kind, from one of the two linked incidents), campaign → captures and links, assignment →
   incident and candidate campaigns. `duplicate_of` points at a canonical capture in any incident. Ids are unique per entity type. Unknown fields are rejected.
6. A link's two endpoints differ. `decision = assign` requires a `campaign_id` among the
   candidates; `new` and `review` must not set one.

## Versioning

`CONTRACT_VERSION = "0.1.0"` is written into every `Dataset` and checked on load. While the
major version is 0, a **minor** bump is breaking: renamed or removed fields, changed enum
members, tighter invariants. A **patch** bump is additive, such as a new optional field or a new indicator `kind` (kinds are
free snake_case strings). New code reads older documents of the same `0.MINOR`. Unknown fields
are rejected, so read a newer-patch document with code at least that new. Any other version
fails validation. v0.1 is frozen. Propose changes to the maintainers
before editing the models.

## Fixture v0 (private; not in git)

There are four real cases with 10 captures and 97 indicators: `do_udostepnienia` 1, 48 and 61,
and `BACKUP` p15. The fixture exercises:

- a multi-capture incident: case 61 has an ad, two landers and a decoy;
- missing modalities: no ad video and no Ad Library entry;
- a shared tracking account: one Meta pixel links cases 1 and p15, together with the
  landing and the tenant;
- collector artefacts: the AIO fingerprint and viewport shared by cases 1 and 48, shown as
  `collector_excluded`;
- embedding references, typed links at three levels, two campaigns, and assignments with
  `assign` and `review` decisions.

Indicators come from the raw files and the signal-extraction outputs. Link scores use
illustrative prior weights, so the fixture shows the contract's shape, not a tuned model. The
vector store it references is not shipped in v0.

```bash
gcloud storage rsync gs://<DATA_BUCKET>/outputs/fixture/v0 data/fixture/v0   # on the VM
```

```python
from ftf.contract.fixture import load_fixture
ds = load_fixture()          # path arg, else $FTF_FIXTURE_DIR, else ./data/fixture/v0
```

`tests/contract/test_fixture.py` validates the fixture and is skipped when the fixture is absent.
To rebuild it on the VM (it reads `/data/raw`):
`python3 -I scripts/build_fixture.py --derived <prototype outputs> --out <dir>`.
