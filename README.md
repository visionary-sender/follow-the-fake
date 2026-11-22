# Follow the Fake

HackNation challenge by NASK PIB ("Kolekcjoner fejków"). Scam ads that abuse the likeness of well-known people usually arrive one at a time. This tool turns single suspicious ads and their landing pages into campaigns: it extracts what each capture shows, deduplicates the captures and groups related incidents, showing the similarities and links in a readable form.

Challenge data lives in a private bucket and never in this repository.

## What it does

- Takes a suspicious ad, a landing page, a URL, a keyword or a batch of them, and extracts what each capture shows: the ad text, the saved page, the network traffic, the redirect chain and public infrastructure facts (registrar, nameservers, certificates).
- Turns each capture into **indicators** (a tracking pixel, a platform tenant, a page-kit fingerprint, a landing id, a domain fact) and scores pairs of captures in **bits of evidence**: how much more likely a shared value is if the two belong to one campaign than if they do not.
- Groups captures into campaigns, one partition per relation (same template, same tracking account, same operator, same campaign, same ecosystem), and assigns a new item to a campaign, to a new campaign, or to a review queue.
- Explains every link with an itemised ledger, labelled with the evidence tiers that CERT Polska uses (Strong, Medium, Supplementary, insufficient alone).
- Ships as the `ftf` command, a Streamlit demo app and a container image. Content features (text embeddings, image hashes, faces) are scaffolding only in this release; see [Limitations](#limitations).

## Quick start

- **Jury:** build and run the container image, see [Container](#container) and the full run instructions in [docs/container.md](docs/container.md).
- **One item:** `ftf analyze INPUT --base dataset.json --out result.json` runs ingestion, enrichment and linking on one ad, page, URL or keyword against a campaign base, and prints the decision (assign, new or review).
- **Campaign tables:** `ftf export --base dataset.json --out DIR` writes the linked dataset as CSV tables, a validated copy of the dataset and `campaign_patterns.md`.
- **Demo app:** `ftf serve --base dataset.json` (needs the `app` extra), see [Run the demo app on the VM](#run-the-demo-app-on-the-vm).
- **Development:** all computation runs on the workbench VM, see [Running the tests on the VM](#running-the-tests-on-the-vm).

Without `--base`, the commands use `$FTF_BASE`, and otherwise the private fixture on the VM, which is not in git.

## Container

The image contains only the installed `ftf` package (no data, no secrets, no git metadata; the
build context is default-deny in `.dockerignore`). It runs as a non-root user, and the
entry point is `ftf`. Building needs internet access, because pip fetches the dependencies.

For the jury, from a clean checkout of the repository root:

```bash
docker build -t ftf .
docker run --rm ftf --help
docker run --rm ftf --version
```

For the team, on the workbench VM. The sync copies the worktree to `/work/<slug>/src`, where
`<slug>` is the worktree directory name (replace it, e.g. `hack-3-container`):

```bash
scripts/vm.sh run -- true      # sync the worktree to the VM
gcloud compute ssh ftf-workbench --project follow-the-fake --zone europe-central2-a \
  --tunnel-through-iap --quiet \
  --command 'cd /work/<slug>/src && sudo docker build -t ftf:dev . && sudo docker run --rm ftf:dev --help'
```

Docker on the VM is a one-time setup: `sudo apt-get install -y docker.io`.
Remove dangling images afterwards with `sudo docker image prune -f`.

The jury run in full (first start with the organiser's folder, a prebuilt base bundle, the
environment variables, offline and online modes, and the image leak check) is in
[docs/container.md](docs/container.md).

## Demo walkthrough

The walkthrough uses the hour-4 slice: a linked campaign base built from the challenge cases on the workbench VM, with one case held out. It runs three items through `ftf analyze`. Cases are named by their folder number.

1. **A known variant.** `do_udostepnienia:1`, held out of the base, is assigned to the 7-incident campaign it belongs to. Its ledger shows which layers carry the 22.7 bits. Collector values appear as excluded rows with 0 bits.
2. **A new campaign.** A submitted landing page with no shared value that carries bits, against any campaign, is declared **new**. Its evidence capacity is 44.4 bits, so the pages it could still show are enough to settle the question.
3. **A screenshot-only item.** A submitted ad screenshot with no landing page (its video and its Ad Library entry are missing) is sent to **review**. Its evidence capacity is 0 bits, far below the threshold, so the system does not call it new. The report says which pages would settle it.

The numbers are the method's output from hand-set weights, not a measured accuracy; see [Evaluation](#evaluation).

## Methodology

The full description is in [docs/methodology.md](docs/methodology.md). Its weights and thresholds are read from the code (`src/ftf/linking/`). In short:

1. **Captures.** A visit or a saved item (ad, pre-lander, lander, decoy) is one capture, with the modalities it has and the ones it lacks. Exact duplicates collapse onto one capture first.
2. **Indicators per layer.** Identifiers and features come from saved files and recorded traffic: tracking accounts, operator and tenant configuration, page-kit fingerprints, landing ids, infrastructure facts and ad text. Each indicator sits in one layer and is marked live (seen in traffic) or static (seen in saved files).
3. **Collector exclusion.** Values that describe the analyst or the capture tool (fingerprints, addresses, user agents, screen sizes, capture times) are excluded by type. They never link anything.
4. **Evidence in bits.** A shared value is worth the log2 of how much more likely it is under "same campaign" than under "different campaign". Rarity is counted over groups of incidents, not over pages. The ids of one operator form a chain (platform, tenant, owner, campaign, landing), and a value is counted once, at the level the data shows it belongs to. The best value of a layer counts, the layers add up, a conflict on a single-valued operator id costs 4 bits and forbids a link, and a missing layer counts 0.
5. **Relations and grouping.** Each of the five relations has its own partition. Clustering is greedy average linkage with cannot-links, so one false match does not chain two campaigns together.
6. **Assign, new or review.** An item is assigned to a campaign when it clears the bar (the threshold plus log2 of the number of campaigns), declared new when it scores at least 3 bits below the threshold and could still reach it, and otherwise sent to review.
7. **Explanation.** Each link carries its ledger: one row per layer and value, with its status, bits, the groups that carry it and the supporting indicator ids.

## Data sources and metadata sources

The challenge data (the case folders, the BACKUP archive and the link list) is the organiser's. It lives in the bucket `gs://follow-the-fake-data` and on the workbench VM, never in this repository. Derived outputs go to the bucket under `outputs/`.

| Source | Provenance | What we take | Terms and limits |
|---|---|---|---|
| Challenge cases | the organiser, in `raw/2026-11-21/` | saved ads, pages, HAR files, screenshots, metadata | Not redistributed. Used for the challenge only. Never in git or in published text. |
| Live pages, through the safe fetcher | pages we fetch in a sandbox VM | HAR, final DOM, screenshot, redirect chain, outcome | GET and HEAD only; no form is submitted and nothing is clicked. Runs only on the sandbox VM. Timeouts and byte budgets apply. Details: [docs/fetcher.md](docs/fetcher.md). |
| Ad libraries, through the scanner | Meta Ad Library page, Google Ads Transparency Center RPC, Google's public BigQuery dataset of removed creatives, the CERT Polska warning list | ad text, advertiser, declared landing domain, creative links, listing dates | Meta's terms forbid automated collection, so the Meta source is a demonstration only; the production route is the official Meta Ad Library API. The BigQuery query is billed to the GCP project that runs it. The CERT Polska list may be used for any purpose, with no attribution required. Details: [docs/scanner.md](docs/scanner.md). |
| Domain and certificate metadata | IANA RDAP bootstrap, registry RDAP servers, DNS over HTTPS (dns.google), crt.sh, hole.cert.pl | registrar, creation date, nameservers, MX, certificate issuer and siblings, CERT Polska listing | Keyless services, each with its own rate limit and cache TTL. A failed lookup never raises and is recorded as `unknown`; a stale cache entry is labelled as stale. Details and terms: [docs/enrichment.md](docs/enrichment.md). |

Metadata sources considered but not used in this release:

- **urlscan.io** hit counts for hashes and file names, to estimate background frequencies. Its queries are built from organiser data, so they need the owner's approval first.
- **Public library hash lists** (cdnjs, jsDelivr), as an extension of the stop list of shared public libraries.
- **The Meta Ad Library API**, the production route for ads, which needs a developer account and identity confirmation.

### Safe URL fetcher (`ftf fetch`)

`ftf fetch URL --out DIR` opens a URL in a locked-down headless Chromium and saves what a visitor would receive: the network traffic (HAR), the final HTML, a full-page screenshot and the redirect chain, as one capture folder that ingestion reads like a case folder. It never fills in or submits a form and never clicks. Dead and cloaked pages are results with an outcome, not errors. A network guard refuses private and metadata addresses and checks every redirect, and the browser runs with no credentials. Details, the outcome categories, the safety model, the sandbox VM and the hardened container run are in [docs/fetcher.md](docs/fetcher.md).

### Ad-library scanner (`ftf scan`)

`ftf scan` searches public ad libraries by keyword and date, and turns each ad it has not seen before into one incident in a queue waiting for assignment. It never opens a landing page; landing URLs go to the sandboxed fetcher. The sources, their terms, the output layout and the scheduled sandbox runs are in [docs/scanner.md](docs/scanner.md).

### Domain enrichment (`ftf enrich-domain`)

`ftf enrich-domain` looks up registrable domains in four keyless services and writes contract indicators in the `infrastructure` layer. It contacts only those services, never the scanned domain's own server, and sends a generic User-Agent. The services, their terms and limits, and the indicator kinds and levels are in [docs/enrichment.md](docs/enrichment.md).

## Evaluation

### Evaluation results (pending HACK-29)

> **Pending.** The full evaluation (HACK-29) has not run. No accuracy figure is reported here, and none should be quoted until it has.

The protocol, from the method notes:

- **Time split.** The base holds the earlier captures, and the later ones are scored as new items. This is the closest proxy we have to a hidden test set.
- **Leave-one-out.** Each incident of a campaign with two or more incidents is removed in turn and assigned against the rest. We report how often the right campaign is chosen, how often a wrong one is accepted (the false-accept rate, which should be zero) and the median evidence capacity.
- **Distractors and degraded inputs.** Pairs that must not link (captures of two different campaigns of one operator, a kit copied onto another platform, a real news article impersonated by a scam) and a synthetic copy of a kit must not join a campaign. Each item is also scored with its traffic, saved page or ad text removed, and a screenshot-only item must end in review. A ±2-bit sweep over every prior flags the links that flip.

The hour-4 slice result, with case numbers only: held-out case `do_udostepnienia:1` is assigned to its 7-incident campaign at 22.7 bits (threshold 10.4); a screenshot-only item goes to review; an unrelated page goes to new.

## Limitations

- **The weights are priors.** They are explicit, hand-set bits, not fitted to the data, and the case sample is 25 scams with no true negatives. Section 9 of [docs/methodology.md](docs/methodology.md) lists what that implies. A learned table (HACK-23) can be passed with `ftf link --weights`, but the default run keeps the hand-set priors.
- **The demo base is small.** The 2026-11-22 slice (a dated snapshot) is small, and the scaling figures in the methodology are design estimates and have not been measured.
- **Content features are scaffolding.** Text embeddings, image hashes and faces do not yet carry bits, and the Visual layer is paused.
- **Enrichment and live fetching depend on outside services.** When a service is down, the item carries less evidence and is more likely to go to review.
- **Campaigns rotate.** Operators change pages, pixels and ad accounts inside a campaign. The campaign relation is kept strict for that reason, so variants can end in review rather than in a campaign.
- **The demo app's "insufficient alone" label** still uses an adapter threshold (log2 of the number of captures) until the engine publishes its threshold in the dataset.
- **The evaluation is pending**, so the method's quality is not yet measured.

## Repository layout

| Path | What |
|---|---|
| `src/ftf/` | the Python package; `ftf` is the command-line entry point |
| `src/ftf/ingest/` | case folders and captures become contract incidents |
| `src/ftf/indicators/` | indicator extractors per layer (kinds in `_ids.py`), including collector traces |
| `src/ftf/linking/` | evidence ledger, weights (hand table and learned), id hierarchy and chain rule, background rarity, relations and grouping, assignment |
| `src/ftf/enrich/` | keyless domain enrichment (`ftf enrich-domain`) |
| `src/ftf/fetch/` | safe URL fetcher (`ftf fetch`) |
| `src/ftf/scanner/` | ad-library scanner (`ftf scan`) |
| `src/ftf/features/` | content features (scaffolding; the Visual layer is paused) |
| `src/ftf/pipeline/` | end-to-end analysis (`ftf analyze`) and the export (`ftf export`); stage adapters in `stages.py` |
| `src/ftf/app/` | Streamlit demo app (`ftf serve`); evidence ledger in `ledger.py`, campaign views in `relations.py` |
| `src/ftf/contract/` | the data contract (pydantic models); see `docs/contract.md` |
| `docs/` | `contract.md`, `methodology.md`, `container.md` (the jury run), and the details of the fetcher, scanner and enrichment |
| `tests/` | pytest suite, run on the workbench VM |
| `scripts/vm.sh` | cloud dev loop: sync to the workbench VM, run tests or commands there |
| `scripts/check_no_organiser_values.py` | fails if organiser values reach tracked files or commit messages |
| `scripts/egress.sh`, `scripts/egress_measure.py` | short-lived egress workers with fresh Google Cloud IPs (corpus building only) and the HACK-45 measurement |
| `scripts/sandbox.sh` | sandbox VM for live fetching |
| `scripts/scanner-sandbox.sh`, `deploy/scanner/` | scheduled ad-library scanner on the sandbox VM |
| `scripts/spend.py` | read-only cloud spend report |
| `scripts/licences.py` | generates and checks `LICENSES-THIRD-PARTY.md` |
| `scripts/build_fixture.py` | rebuilds the private fixture on the VM |
| `docker/seccomp-chromium.json` | seccomp profile that lets Chromium's sandbox run inside the container |
| `Dockerfile`, `.dockerignore` | container image that runs the `ftf` command |

## Tests

### Running the tests on the VM

All computation runs on the workbench VM, not on the laptop.

Prerequisites:

- `gcloud` with the configuration `follow-the-fake` (the script sets
  `CLOUDSDK_ACTIVE_CONFIG_NAME=follow-the-fake` if it is unset) and IAP SSH access to the VM
  `ftf-workbench` (`europe-central2-a`). The VM must be running.
- `git` and `tar` locally.

```bash
scripts/vm.sh test                 # run the whole suite on the VM; exits with pytest's code
scripts/vm.sh test -k cli -x       # extra arguments go to pytest
scripts/vm.sh run -- ftf --help    # any command in the synced checkout, venv active
scripts/vm.sh licences             # regenerate LICENSES-THIRD-PARTY.md
```

Each call copies the worktree's tracked and untracked-but-not-ignored files (never `data/` or
anything gitignored) to `/work/<worktree-dir-name>/src` on the VM, replacing the previous copy.
The venv `/work/<worktree-dir-name>/venv` is pinned to the VM's base package versions and is
rebuilt only when `pyproject.toml` changes. Different worktrees get different directories, so
they can use the VM at the same time; set `FTF_VM_SLUG` to choose the name yourself. The script
refuses to take over an existing `/work/<name>` that it did not create.

### Organiser-value check

`scripts/check_no_organiser_values.py` fails if a domain, host, URL, id-like indicator value or page
title of the organiser dataset appears in a tracked file or in a commit message. The dataset exists
only on the workbench VM, so the check runs there. Write the commit messages on the laptop, copy them
to the VM with `gcloud compute scp`, then run:

```bash
git log --all --format=%B > commit-messages.txt        # on the laptop
scripts/vm.sh run -- true                              # sync the worktree
gcloud compute scp commit-messages.txt ftf-workbench:/work/<slug>/ --project follow-the-fake \
  --zone europe-central2-a --tunnel-through-iap --quiet
scripts/vm.sh run -- python3 -I scripts/check_no_organiser_values.py \
  --dataset /data/derived/ingest/organiser/dataset.json --root . \
  --messages /work/<slug>/commit-messages.txt          # <slug> is the worktree directory name
```

A hit prints `file:line`, the kind of value and a short hash of the match, never the value itself.
Exit 0 means no hit, and exit 1 means at least one. Values shorter than 6 characters and a short
allowlist of public services are ignored.

## Licences

### Dependencies and licences

Only permissively licensed open-source dependencies are allowed (MIT, BSD, Apache, PSF, ISC, HPND).
`LICENSES-THIRD-PARTY.md` is generated from installed package metadata by
`scripts/vm.sh licences`; a test fails if the table is stale or lists a licence outside the
allowlist. After changing dependencies in `pyproject.toml`, regenerate it and commit it.

### Licence

The code in this repository is not under an open licence: all rights are reserved by its authors.
Under the HackNation competition rules, the economic copyright to a winning solution is transferred
to NASK PIB. Third-party dependencies are permissive open-source software, listed with their
licences in `LICENSES-THIRD-PARTY.md` (generated by `scripts/vm.sh licences`).

`certifi` (MPL-2.0, a CA bundle, pulled in by `requests` through `tldextract`) is the one named
exception to the permissive-only rule: it is used unmodified, MPL-2.0 is file-level copyleft, and it
places no obligation on our code. The exception is listed by package in `scripts/licences.py`; other
MPL packages stay denied.

The licence rule applies to what ships in the jury's container; tools used only to collect data
during the build are not a licensing concern. Playwright, its Chromium (with Chromium's bundled,
unmodified LGPL-2.1 FFmpeg) and its Node.js driver are a named exception (owner, 2026-11-21); they
come only with the optional `fetch` extra and are listed separately in `LICENSES-THIRD-PARTY.md`.

System packages in the image, models (`FTF_WITH_MODELS`, off by default) and outside services, with
their licences and usage limits: [docs/licences.md](docs/licences.md). Some system libraries that
Chromium needs are LGPL or MPL; they are not covered by a named exception yet and await an owner
decision (listed in that document).

## Operations

### Export (`ftf export`)

`ftf export --base dataset.json --out DIR` writes the linked dataset as flat tables: `incidents.csv`,
`captures.csv`, `indicators.csv`, `links.csv`, `evidence.csv` (one row per ledger row of every link)
and `campaigns.csv`. It also writes `dataset.json`, a validated copy of the contract document, and
`campaign_patterns.md`, one generated section per campaign with its size, date span, the relations
that hold it, its stages, its missing modalities and its top shared indicators by bits. The Markdown
defangs every value. The CSV and JSON files keep values as they are, so an export built from organiser
data belongs in the private bucket and never in git.

### Run the demo app on the VM

The app needs the `app` extra (`pip install -e '.[app]'`; `scripts/vm.sh` installs it). The
campaign base is a contract `dataset.json`: `--base`, else `$FTF_BASE`, else the fixture
(`$FTF_FIXTURE_DIR`). On the workbench it binds to `127.0.0.1` only; view it through an IAP tunnel.

```bash
scripts/vm.sh run -- env FTF_FIXTURE_DIR=/data/derived/fixture-v0 ftf serve --port 8501
# second terminal on the laptop, then open http://localhost:8501
gcloud compute ssh ftf-workbench --project follow-the-fake --zone europe-central2-a \
  --tunnel-through-iap -- -N -L 8501:127.0.0.1:8501
```

The same analysis without the UI: `ftf analyze INPUT [--kind K] [--base PATH] [--online] [--out result.json]`
prints a stage summary and writes a contract dataset (exit 0 for assign, new and review).
Inputs are copied to a per-run directory (`$FTF_RUN_DIR`, else the system temp dir) and
hashed, never executed; captured HTML is never rendered, and URLs are shown defanged.
Enrichment and fetching are offline unless `--online` is given.

### Cloud spend

```bash
python3 -I scripts/spend.py
```

Read-only report of what GCP itself records (resource state plus the admin-activity and
system-event audit logs), per project, since 2026-11-21: every VM and disk of ours, including
deleted ones, with run intervals, an estimated cost in USD and PLN, the total against the
100 PLN cap, the split by the issue label, unlabelled resources as a warning, and everything
still running with the time its automatic stop fires. It only lists and reads; it never
creates, changes or deletes anything. Standard library only; it runs on the laptop because it
is just gcloud calls plus arithmetic.

- Costs are an **estimate**: pinned on-demand list prices (`VM_USD_PER_HOUR`,
  `DISK_USD_PER_GB_MONTH` at the top of the script) and a fixed USD to PLN rate of 4.00, so the
  same inputs give the same numbers. Buckets, egress and taxes are not included. A machine or
  disk type missing from the table is flagged and counted as 0 until it is added.
- `follow-the-fake` is read with gcloud configuration `follow-the-fake`; launches carry the
  label `ftf-issue=hack-<n>`.
- `attic-212115` is read with gcloud configuration `default` (override with
  `FTF_ATTIC_GCLOUD_CONFIG`). Nothing there may name this project, so our resources use names
  in the numeric range `8xxxxxxx` (e.g. `vm-80000034-1`) and the label `k=<issue number>`
  (e.g. `k=34`); only resources matching either are counted. This convention is provisional
  and set in one place (`ATTIC_LABEL_KEY`, `ATTIC_NAME_PATTERN`).
- Backstop: billing data lags by hours, no BigQuery billing export is set up and no GCP budget
  alert is known on the billing account (the Budget API is not readable here), so the script is the primary check.
