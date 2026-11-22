# Follow the Fake

HackNation challenge by NASK PIB ("Kolekcjoner fejków"). Scam ads that abuse the likeness of well-known people usually arrive one at a time. This tool turns single suspicious ads and their landing pages into campaigns: it extracts what each capture shows, deduplicates the captures and groups related incidents, showing the similarities and links in a readable form.

Challenge data lives in a private bucket and never in this repository.

## What it does

- Takes a suspicious ad, a landing page, a URL, a keyword or a batch of them, and extracts what each capture shows: the ad text, the saved page, the network traffic, the redirect chain and public infrastructure facts (registrar, nameservers, certificates).
- Turns each capture into **indicators** (a tracking pixel, a platform tenant, a page-kit fingerprint, a landing id, a domain fact) and scores pairs of captures in **bits of evidence**: how much more likely a shared value is if the two belong to one campaign than if they do not.
- Groups captures into campaigns, one partition per relation (same template, same tracking account, same operator, same campaign, same ecosystem), and assigns a new item to a campaign, to a new campaign, or to a review queue.
- Explains every link with an itemised ledger, labelled with the evidence tiers that CERT Polska uses (Strong, Medium, Supplementary, insufficient alone).
- Ships as the `ftf` command, a Streamlit demo app and a container image. Content features are merged and on: text (OCR, page text, masked templates, embeddings), images (perceptual hashes, embeddings), video keyframes and speech, people and brands, and faces (YuNet detection, SFace embeddings, an 11-person reference set). Calibrated content views that feed the linking ledger are off by default; see [Limitations](#limitations).

## Quick start

- **Jury:** build and run the container image, see [Container](#container) and the full run instructions in [docs/container.md](docs/container.md).
- **One item:** `ftf analyze INPUT --base dataset.json --out result.json` runs ingestion, enrichment and linking on one ad, page, URL or keyword against a campaign base, and prints the decision (assign, new or review).
- **Campaign tables:** `ftf export --base dataset.json --out DIR` writes the linked dataset as CSV tables, a validated copy of the dataset and `campaign_patterns.md`.
- **Demo app:** `ftf serve --base dataset.json` (needs the `app` extra), see [Run the demo app on the VM](#run-the-demo-app-on-the-vm).
- **Development:** all computation runs on the workbench VM, see [Running the tests on the VM](#running-the-tests-on-the-vm).

Without `--base`, the commands use `$FTF_BASE`, and otherwise the private fixture on the VM, which is not in git.

## Container

The image contains the installed `ftf` package and the hashed brand stop-list from
`docker/brand-stoplist/` (see the data section above), but no organiser data, no secrets and no git
metadata; the build context is default-deny in `.dockerignore`. It runs as a non-root user, and the
entry point is `ftf`. Building needs internet access, because pip and apt fetch the dependencies.
The image is 1.63 GB and includes Playwright's headless Chromium with its Debian system libraries,
which `ftf fetch` needs. Run it with the hardening flags in [docs/container.md](docs/container.md).

For the jury, from a clean checkout of the repository root (fetch the brand stop-list first, see
the data section below and [docs/container.md](docs/container.md)):

```bash
scripts/fetch-brand-stoplist.sh   # on the workbench VM; the bucket is private
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

The walkthrough uses the hour-4 slice: a linked campaign base (`base-h4-main`) built from the challenge cases on the workbench VM, with case 1 held out. It runs three items through `ftf analyze` on current main. The decisions were the same on the VM and in the container image. Cases are named by their folder number.

1. **A known variant.** `do_udostepnienia:1`, held out of the base, is assigned (`assign`) to the 7-capture campaign it belongs to. The best candidate scores 18.7 bits and the evidence capacity is 48.4 bits. Collector values appear as excluded rows with 0 bits.
2. **A new campaign.** An unrelated saved page, with no shared value against any campaign, is declared **new** (`new`). Its evidence capacity is 44.9 bits, so the pages it could still show are enough to settle the question.
3. **A screenshot-only item.** The ad screenshot of case 1, submitted alone with no landing page and no traffic, is sent to **review** (`review`). Its evidence capacity is 0 bits, far below the threshold, so the system does not call it new.

The numbers are the method's output from hand-set weights, not a measured accuracy; see [Evaluation](#evaluation).

## Methodology

The full description is in [docs/methodology.md](docs/methodology.md); decisions that need explaining (such as "review" when the platform layer is hidden) are in [docs/linking-decisions.md](docs/linking-decisions.md). Its weights and thresholds are read from the code (`src/ftf/linking/`). In short:

1. **Captures.** A visit or a saved item (ad, pre-lander, lander, decoy) is one capture, with the modalities it has and the ones it lacks. Exact duplicates collapse onto one capture first.
2. **Indicators per layer.** Identifiers and features come from saved files and recorded traffic: tracking accounts, operator and tenant configuration, page-kit fingerprints, landing ids, infrastructure facts and ad text. Each indicator sits in one layer and is marked live (seen in traffic) or static (seen in saved files).
3. **Collector exclusion.** Values that describe the analyst or the capture tool (fingerprints, addresses, user agents, screen sizes, capture times) are excluded by type. They never link anything.
4. **Evidence in bits.** A shared value is worth the log2 of how much more likely it is under "same campaign" than under "different campaign". Rarity is counted over groups of incidents, not over pages. The ids of one operator form a chain (platform, tenant, owner, campaign, landing), and a value is counted once, at the level the data shows it belongs to. The best value of a layer counts, the layers add up, a conflict on a single-valued operator id costs 4 bits and forbids a link, and a missing layer counts 0. Three rules sit on top of the sum. The platform and creative gate (HACK-66) lets a campaign, operator or tracking-account pair link only when one positive row lies outside the platform and creative layers, below platform level: an ad, a celebrity's face or a platform constant alone never links. A brand stop-list, on by default (HACK-66), drops the values that the real outlet pages in the background corpus share with the scam clones. Calibrated content views (HACK-24) are off by default; with `--content-views` they add bits for similar creatives and never open a gate.
5. **Relations and grouping.** Each of the five relations has its own partition. Clustering is greedy average linkage with cannot-links, so one false match does not chain two campaigns together.
6. **Assign, new or review.** An item is assigned to a campaign when it clears the bar (the threshold plus log2 of the number of campaigns), declared new when it scores at least 3 bits below the threshold and could still reach it, and otherwise sent to review. A missing input that could lift the best score to the bar sends the item to review, not new (HACK-67), and a tie within 1 bit at the top goes to review, naming both campaigns (HACK-64).
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

### Brand stop-list (`FTF_BRAND_STOPLIST`)

The linking engine reads a brand stop-list: hashed values (digests only) seen on real outlet pages of
our public corpus, batch 1. A stop-listed value is the impersonated outlet's own skin (logo, fonts,
favicon, its analytics or CDN), so it never links a campaign below the ecosystem relation. The file
is built with `ftf brand-stoplist` and lives in the private bucket under `outputs/linking/brand-stoplist/`.
The image ships it: `scripts/fetch-brand-stoplist.sh` copies it into the build context before
`docker build`, and the container sets `FTF_BRAND_STOPLIST` to the copied file. Without the file the
build still works and the engine warns and scores without the stop-list. Details:
[docs/container.md](docs/container.md) and [docs/linking-decisions.md](docs/linking-decisions.md).

### Face reference set (`FTF_FACE_REFERENCE`)

The faces feature family (`ftf.features.faces`) compares every face in an ad creative or landing page with a small reference set of public figures whose likeness scam ads use. Each face gets its nearest reference people (`person_face`: the top three candidates by cosine similarity of 128-dimension SFace embeddings), so the same face can be linked across captures. No threshold is applied here: the linking engine weighs `person_face` low, because one celebrity is used by many unrelated campaigns.

The set covers 33 public figures with 96 portraits, as listed in `src/ftf/features/faces/reference_manifest.json`:
Adam Glapiński, Andrzej Domański, Andrzej Duda, Anita Werner, Bogdan Rymanowski, Cristiano Ronaldo, Donald Tusk, Elon Musk, Iga Świątek, Jarosław Kaczyński, Justyna Kowalczyk, Karol Nawrocki, Krzysztof Stanowski, Kuba Wojewódzki, Leszek Balcerowicz, Mateusz Morawiecki, Michał Sołowow, Monika Olejnik, Piotr Kraśko, Radosław Sikorski, Rafał Brzoska, Robert Kubica, Robert Lewandowski, Robert Makłowicz, Szymon Hołownia, Sławomir Mentzen, Tomasz Lis, Tomasz Sekielski, Ursula von der Leyen, Wojciech Szczęsny, Wołodymyr Zełenski, Władysław Kosiniak-Kamysz, Zbigniew Boniek.

The set is built on a VM with `python -m ftf.features.faces.reference build` from Wikidata and Wikimedia Commons. Only CC0 and CC BY images are used, each with exactly one clear face, and no organiser image is ever used as a reference. The images and embeddings are not in git. The prebuilt set lives in `gs://follow-the-fake-data/outputs/visual/face-reference/`, and the container image bakes it into `/opt/ftf-models/face-reference`.

The reference directory is resolved in this order:

1. `FTF_FACE_REFERENCE`, if set;
2. `FTF_FACES_REFERENCE`, the older name, still read with a deprecation warning;
3. `$FTF_MODEL_DIR/face-reference` (default `~/.cache/ftf/models/face-reference`; in the container `/opt/ftf-models/face-reference`).

Nothing is downloaded at scan time. Without a manifest in that directory, faces are still embedded and linked, but no `person_face` is written. Details: [docs/features/faces.md](docs/features/faces.md).

### Safe URL fetcher (`ftf fetch`)

`ftf fetch URL --out DIR` opens a URL in a locked-down headless Chromium and saves what a visitor would receive: the network traffic (HAR), the final HTML, a full-page screenshot and the redirect chain, as one capture folder that ingestion reads like a case folder. It never fills in or submits a form and never clicks. Dead and cloaked pages are results with an outcome, not errors. A network guard refuses private and metadata addresses and checks every redirect, and the browser runs with no credentials. Details, the outcome categories, the safety model, the sandbox VM and the hardened container run are in [docs/fetcher.md](docs/fetcher.md).

### Ad-library scanner (`ftf scan`)

`ftf scan` searches public ad libraries by keyword and date, and turns each ad it has not seen before into one incident in a queue waiting for assignment. It never opens a landing page; landing URLs go to the sandboxed fetcher. The sources, their terms, the output layout and the scheduled sandbox runs are in [docs/scanner.md](docs/scanner.md).

### Domain enrichment (`ftf enrich-domain`)

`ftf enrich-domain` looks up registrable domains in four keyless services and writes contract indicators in the `infrastructure` layer. It contacts only those services, never the scanned domain's own server, and sends a generic User-Agent. The services, their terms and limits, and the indicator kinds and levels are in [docs/enrichment.md](docs/enrichment.md).

## Evaluation

### Evaluation results

The protocol, from the method notes:

- **Time split (E7).** The base holds the earlier captures, and the later ones are scored as new items. This is the closest proxy we have to a hidden test set.
- **Hidden-layer recovery (E1).** The platform ids are hidden, and the engine must recover campaign and operator pairs from the other layers. It is scored on pairwise counts, macro B-cubed precision and recall, and must-not-link violations.
- **Leave-one-out (E2).** Each incident of a campaign with two or more incidents is removed in turn and assigned against the rest. We report how often the right campaign is chosen, how often a wrong one is accepted (the false-accept rate, which should be zero) and the median evidence capacity.
- **Degraded inputs (E3).** The held-out item loses one modality at a time (traffic, saved page, ad text or screenshot), and a URL-only mode keeps just the link. A screenshot-only item must end in review, not new.
- **Distractors (E4).** Pairs that must not link (captures of two different campaigns of one operator, a kit copied onto another platform, a real outlet page used for a brand, a page with only platform constants, a page with only a celebrity) and a synthetic copy of a kit must not join a campaign.
- **Sensitivity (E6).** Each prior is moved by −2, −1, +1 and +2 bits, one at a time. The links that flip are flagged as fragile.

The tables below are the final evaluation report (HACK-29, `report.md` in `gs://follow-the-fake-data/outputs/evaluation/report/`), copied from its file with only the heading levels changed. Each table states its evidence setting, and cases are named by folder number only. The counts are raw, so read them as counts, not as accuracy.

Two results matter most for the claims above, and the tables below give the counts:

- With platform ids hidden (E1), the Linking engine recovers 11 of 11 true campaign pairs, and all 11 of its predicted campaign pairs are true. Operator and tenant recall is lower (6 of 22 and 6 of 23 pairs), with every predicted pair true, and it violates no must-not-link pair (0 of 37).
- On the distractors (E4), the Linking engine passes every class except the synthetic kit copy (b), which is reported in its table rather than hidden.

In the pair counts, `P` is out of the predicted pairs and `R` is out of the true pairs, as the report defines them.

**Cases and incidents.** The evaluation counts 26 case folders. The organiser dataset has 28 incidents: the 25 case folders (15 in `BACKUP`, 10 in `do_udostepnienia`) and 3 ad-hoc inputs, each one URL from the organiser's scam-link list. The report's 26 case folders are those 25 plus one group for the three scam-link inputs. The E7 time split and the `input:` rows treat the three inputs as separate incidents.

<!-- eval:build start -->
Dataset build: `main 8eec049 (ingest; adds the HACK-79 yt-dlp extractor) + Visual features (all four families: text, image, faces, video; src/ftf/features unchanged 2dd0fd8..8eec049)`. Each table title states its evidence setting: E1, E6 and E7 run with platform ids hidden; E2, E3 and E4 run on all evidence (platform ids visible), the realistic setting. The labels and leak check use the platform ids as ground truth. Counts are raw: `P 2 of 11 pairs` means 2 of the 11 predicted pairs are true, and `R 2 of 12 pairs` means 2 of the 12 true pairs were found. Macro scores average over label groups first, then over groups. `baseline` is the loose floor; `linking` is the Linking engine.
<!-- eval:build end -->

<!-- eval:ground-truth start -->
#### Ground truth and leak check

| item | count |
|---|---|
| captures (with duplicates) | 34 |
| canonical captures | 33 |
| case folders (incidents) | 26 |
| analyst must-not-link pairs | 44 |
| leak check hits (expected 0) | 0 |
| hidden indicators (removed) | 164 |

| level | groups | non-singleton groups | captures |
|---|---|---|---|
| analyst_campaign | 1 | 1 | 2 |
| analyst_operator | 1 | 1 | 3 |
| analyst_template | 3 | 3 | 9 |
| campaign | 7 | 3 | 13 |
| landing | 9 | 2 | 14 |
| owner | 5 | 3 | 13 |
| template | 4 | 3 | 13 |
| tenant | 4 | 3 | 14 |
<!-- eval:ground-truth end -->

<!-- eval:e1-hidden start -->
#### E1 hidden-layer recovery (platform ids hidden)

| relation vs level | baseline pairwise | linking pairwise | baseline macro B-cubed P / R | linking macro B-cubed P / R | must-not-link violated, baseline | must-not-link violated, linking |
|---|---|---|---|---|---|---|
| campaign vs campaign | P 11 of 66 pairs, R 11 of 11 pairs | P 11 of 11 pairs, R 11 of 11 pairs | 0.14 / 1.00 over 7 groups | 1.00 / 1.00 over 7 groups | 34 of 37 pairs | 0 of 37 pairs |
| operator vs owner | P 22 of 66 pairs, R 22 of 22 pairs | P 6 of 6 pairs, R 6 of 22 pairs | 0.20 / 1.00 over 5 groups | 1.00 / 0.78 over 5 groups | 1 of 1 pairs | 0 of 1 pairs |
| operator vs tenant | P 23 of 66 pairs, R 23 of 23 pairs | P 6 of 6 pairs, R 6 of 23 pairs | 0.25 / 1.00 over 4 groups | 1.00 / 0.60 over 4 groups | 1 of 1 pairs | 0 of 1 pairs |
<!-- eval:e1-hidden end -->

<!-- eval:e7-timesplit start -->
#### E7 time split (reference = 8 October captures; platform ids hidden)

| engine | reference folders | later items | correct assignment | wrong assignment | false accept | correct new | missed (new, should assign) | review | bridging folders | fragmented groups |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 15 | 13 | 3 | 0 | 7 | 3 | 0 | 0 | 12 | 0 |
| linking | 15 | 13 | 2 | 0 | 0 | 5 | 1 | 5 | 12 | 0 |

| case folder | date | expected | baseline verdict | linking verdict |
|---|---|---|---|---|
| do_udostepnienia:5 | - | new | false accept | review |
| input:15c96e9a0fe4 | - | new | correct new | review |
| input:7a4b9bcf6948 | - | new | correct new | review |
| input:dff1e493c8d3 | - | new | correct new | review |
| do_udostepnienia:1 | 2026-10-09 | assign to BACKUP:p1, BACKUP:p15, BACKUP:p7 | correct assignment | correct assignment |
| do_udostepnienia:2 | 2026-10-09 | assign to BACKUP:p1, BACKUP:p15, BACKUP:p7 | correct assignment | correct assignment |
| do_udostepnienia:3 | 2026-10-09 | assign to BACKUP:p1, BACKUP:p15, BACKUP:p7 | correct assignment | missed (new, should assign) |
| do_udostepnienia:10 | 2026-10-12 | new | false accept | correct new |
| do_udostepnienia:14 | 2026-10-12 | new | false accept | correct new |
| do_udostepnienia:36 | 2026-10-19 | new | false accept | correct new |
| do_udostepnienia:61 | 2026-10-20 | new | false accept | correct new |
| do_udostepnienia:47 | 2026-10-27 | new | false accept | review |
| do_udostepnienia:48 | 2026-10-28 | new | false accept | correct new |
<!-- eval:e7-timesplit end -->

<!-- eval:e2-openset start -->
#### E2 leave-one-out open-set assignment (all evidence)

| engine | protocol | mode | items | correct | wrong campaign | new | review | assigned | median capacity (bits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | loco | full | 8 | 0 | 8 | 0 | 0 | 8 | 153.00 |
| baseline | loco_mixed | full | 2 | 1 | 1 | 0 | 0 | 2 | 223.00 |
| baseline | loo_item | full | 8 | 5 | 3 | 0 | 0 | 8 | 153.00 |
| linking | loco | full | 8 | 0 | 0 | 8 | 0 | 0 | 48.40 |
| linking | loco_mixed | full | 2 | 1 | 0 | 0 | 1 | 1 | 48.40 |
| linking | loo_item | full | 8 | 7 | 0 | 0 | 1 | 7 | 48.40 |

Counts are per item; a leave-one-group item stands for one whole group. A wrong campaign is an assignment into a campaign the item does not belong to; false accepts (an assignment of an item that should be new) are counted in E7 only.
<!-- eval:e2-openset end -->

<!-- eval:e3-degraded start -->
#### E3 degraded inputs, the held-out item loses one modality (all evidence)

| engine | protocol | mode | items | correct | wrong campaign | new | review | assigned | median capacity (bits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | loco | ad_text | 8 | 0 | 0 | 8 | 0 | 0 | 0.00 |
| baseline | loco | full | 8 | 0 | 8 | 0 | 0 | 8 | 153.00 |
| baseline | loco | har | 8 | 0 | 7 | 1 | 0 | 7 | 65.00 |
| baseline | loco | html | 8 | 0 | 8 | 0 | 0 | 8 | 64.50 |
| baseline | loco | screenshot | 8 | 0 | 6 | 2 | 0 | 6 | 6.00 |
| baseline | loco | url | 8 | 0 | 0 | 8 | 0 | 0 | 0.00 |
| baseline | loco_mixed | ad_text | 2 | 0 | 0 | 2 | 0 | 0 | 0.00 |
| baseline | loco_mixed | full | 2 | 1 | 1 | 0 | 0 | 2 | 223.00 |
| baseline | loco_mixed | har | 2 | 1 | 1 | 0 | 0 | 2 | 154.00 |
| baseline | loco_mixed | html | 2 | 1 | 1 | 0 | 0 | 2 | 62.00 |
| baseline | loco_mixed | screenshot | 2 | 0 | 0 | 2 | 0 | 0 | 0.00 |
| baseline | loco_mixed | url | 2 | 0 | 0 | 2 | 0 | 0 | 0.00 |
| baseline | loo_item | ad_text | 8 | 0 | 0 | 8 | 0 | 0 | 0.00 |
| baseline | loo_item | full | 8 | 5 | 3 | 0 | 0 | 8 | 153.00 |
| baseline | loo_item | har | 8 | 5 | 2 | 1 | 0 | 7 | 65.00 |
| baseline | loo_item | html | 8 | 5 | 3 | 0 | 0 | 8 | 63.00 |
| baseline | loo_item | screenshot | 8 | 4 | 1 | 3 | 0 | 5 | 6.00 |
| baseline | loo_item | url | 8 | 0 | 0 | 8 | 0 | 0 | 0.00 |
| linking | loco | ad_text | 8 | 0 | 0 | 0 | 8 | 0 | 0.00 |
| linking | loco | full | 8 | 0 | 0 | 8 | 0 | 0 | 48.40 |
| linking | loco | har | 8 | 0 | 0 | 4 | 4 | 0 | 48.40 |
| linking | loco | html | 8 | 0 | 0 | 8 | 0 | 0 | 44.90 |
| linking | loco | screenshot | 8 | 0 | 0 | 0 | 8 | 0 | 4.00 |
| linking | loco | url | 8 | 0 | 0 | 0 | 8 | 0 | 0.00 |
| linking | loco_mixed | ad_text | 2 | 0 | 0 | 0 | 2 | 0 | 0.00 |
| linking | loco_mixed | full | 2 | 1 | 0 | 0 | 1 | 1 | 48.40 |
| linking | loco_mixed | har | 2 | 1 | 0 | 0 | 1 | 1 | 48.40 |
| linking | loco_mixed | html | 2 | 0 | 0 | 2 | 0 | 0 | 44.90 |
| linking | loco_mixed | screenshot | 2 | 0 | 0 | 0 | 2 | 0 | 0.00 |
| linking | loco_mixed | url | 2 | 0 | 0 | 0 | 2 | 0 | 0.00 |
| linking | loo_item | ad_text | 8 | 0 | 0 | 0 | 8 | 0 | 0.00 |
| linking | loo_item | full | 8 | 7 | 0 | 0 | 1 | 7 | 48.40 |
| linking | loo_item | har | 8 | 7 | 0 | 0 | 1 | 7 | 48.40 |
| linking | loo_item | html | 8 | 0 | 0 | 5 | 3 | 0 | 44.90 |
| linking | loo_item | screenshot | 8 | 0 | 0 | 0 | 8 | 0 | 4.00 |
| linking | loo_item | url | 8 | 0 | 0 | 0 | 8 | 0 | 0.00 |
<!-- eval:e3-degraded end -->

<!-- eval:e4-distractors start -->
#### E4 distractors (all evidence)

| distractor | expected | baseline passed | linking passed |
|---|---|---|---|
| natural must-not-link pair (a) | no link or campaign of relation campaign between the subjects and the paired capture | 3 of 43 | 43 of 43 |
| synthetic kit copy (b) | no link or campaign of relation campaign, operator, tracking_account between the subjects and outside captures; never assigned (new or review); expected template link to the original (reported, not required) | 0 of 3 | 2 of 3 |
| platform-only page (c) | no link or campaign of relation campaign, operator, template, tracking_account between the subjects and outside captures; never assigned (new or review) | 0 of 3 | 3 of 3 |
| collector invariance (injected fingerprint) (d) | zero decisions change | 2 of 2 | 2 of 2 |
| brand negative (e) | no link or campaign of relation campaign, operator, template, tracking_account between the subjects and outside captures; never assigned (new or review) | 9 of 58 | 58 of 58 |
| celebrity-only context (f) | no link or campaign of relation campaign, operator, template, tracking_account between the subjects and outside captures; never assigned (new or review) | 0 of 2 | 2 of 2 |

Must-not-link pairs violated: baseline 40 of 43, linking 0 of 43.
<!-- eval:e4-distractors end -->

<!-- eval:e6-sensitivity start -->
#### E6 sensitivity (linking table, moves of −2, −1, +1, +2 bits; platform ids hidden)

Weights tested: 39 of 91 in the table; 16 flip no decision.

| relation | pair links (unperturbed) | pair flips, all moves | fragile links (±2, one layer) | worst-case only |
|---|---|---|---|---|
| campaign | 13 | 67 | 10 of 13 | 13 |
| ecosystem | 12 | 209 | 4 of 12 | 12 |
| operator | 7 | 55 | 6 of 7 | 7 |
| template | 1 | 21 | 0 of 1 | 1 |
| tracking_account | 0 | 3 | 0 of 0 | 0 |

E7 verdicts, unperturbed: assign 2, new 6, review 5.
A cell `n (eff. x)` is a move that the 0-bit floor clamped: x is the change really made, in bits.

| weight | bits | −2 | −1 | +1 | +2 | total |
|---|---|---|---|---|---|---|
| kind:inline_script_sha256 | 3 | 8 | 6 | 18 | 28 | 60 |
| kind:third_party_host | 1.5 | 15 (eff. -1.5) | 15 | 8 | 9 | 47 |
| kind:image_phash | 3 | 10 | 8 | 6 | 21 | 45 |
| kind:aio_sdk_asset | 1.7 | 4 (eff. -1.7) | 4 | 9 | 17 | 34 |
| kind:asset_norm_sha256 | 2.5 | 0 | 0 | 9 | 18 | 27 |
| kind:form_field_set | 2 | 0 | 0 | 10 | 16 | 26 |
| kind:third_party_domain | 1.5 | 0 (eff. -1.5) | 0 | 9 | 17 | 26 |
| kind:dom_skeleton_sha256 | 3.5 | 8 | 6 | 0 | 10 | 24 |
| kind:asset_sha256 | 2.5 | 0 | 0 | 6 | 16 | 22 |
| kind:form_endpoint | 2 | 0 | 0 | 8 | 8 | 16 |

Fragile links, by case folder (a fragile link flips under one layer moved ±2 bits):

| relation | case folder a | case folder b | state | flips when |
|---|---|---|---|---|
| operator | BACKUP:p1 | BACKUP:p3 | link | tracking_account -2 to review; kit -2 to review; platform -2 to review; infrastructure -2 to review |
| operator | BACKUP:p1 | BACKUP:p4 | link | tracking_account -2 to review; kit -2 to review; platform -2 to review; infrastructure -2 to review |
| operator | BACKUP:p1 | BACKUP:p7 | link | tracking_account -2 to review; infrastructure -2 to review |
| operator | BACKUP:p3 | BACKUP:p4 | link | tracking_account -2 to review |
| operator | BACKUP:p3 | BACKUP:p7 | link | tracking_account -2 to review; kit -2 to review; platform -2 to review; infrastructure -2 to review |
| operator | BACKUP:p4 | BACKUP:p7 | link | tracking_account -2 to review; kit -2 to review; platform -2 to review; infrastructure -2 to review |
| campaign | BACKUP:p15 | BACKUP:p1 | link | creative -2 to review; kit -2 to no link |
| campaign | BACKUP:p15 | BACKUP:p7 | link | creative -2 to review; kit -2 to no link |
| campaign | BACKUP:p15 | do_udostepnienia:1 | link | kit -2 to review |
| campaign | BACKUP:p15 | do_udostepnienia:2 | link | kit -2 to review |
| campaign | BACKUP:p1 | do_udostepnienia:1 | link | creative -2 to review; kit -2 to no link |
| campaign | BACKUP:p1 | do_udostepnienia:2 | link | creative -2 to review; kit -2 to no link |
| campaign | BACKUP:p7 | do_udostepnienia:1 | link | creative -2 to review; kit -2 to no link |
| campaign | BACKUP:p7 | do_udostepnienia:2 | link | creative -2 to review; kit -2 to no link |
| campaign | do_udostepnienia:1 | do_udostepnienia:2 | link | kit -2 to review |
| campaign | do_udostepnienia:2 | do_udostepnienia:3 | link | kit -2 to review |
| ecosystem | BACKUP:p1 | BACKUP:p3 | link | kit -2 to review |
| ecosystem | BACKUP:p1 | BACKUP:p4 | link | kit -2 to review |
| ecosystem | BACKUP:p3 | BACKUP:p7 | link | kit -2 to review |
| ecosystem | BACKUP:p4 | BACKUP:p7 | link | kit -2 to review |
<!-- eval:e6-sensitivity end -->

<!-- eval:run-status start -->
#### Run status

| step | status |
|---|---|
| labels | ok |
| leak | ok |
| hidden baseline | ok |
| hidden linking | ok |
| timesplit baseline | ok |
| timesplit linking | ok |
| sensitivity | ok |
| openset baseline | ok |
| openset linking | ok |
| degrade baseline | ok |
| degrade linking | ok |
| distractors baseline | ok |
| distractors linking | ok |
<!-- eval:run-status end -->

## Limitations

<!-- eval:limitations start -->
The sample is small: 33 canonical captures in 26 case folders, 3 non-singleton campaign groups and 13 later items, so these counts are a regression check, not a benchmark. Cluster sizes are skewed: the largest campaign group holds 5 of 33 captures, so pair counts are dominated by it and macro-averages over groups are reported next to them. The E7 time split uses the organiser's source set instead of a capture date for 4 of 13 later items, so it tests organiser sets rather than dates. The reference base is not grown during E7: each later item is assigned against the reference folders alone, so later items never join one another or the base. The synthetic distractors (E4) are built from one organiser batch, so they test the engine on controlled edits rather than on other sources. The brand negatives are one batch of pages, so a clean E4 result says little about other brands or sources. Open-set assignment (E2, all evidence) gives 0 wrong-campaign assignments out of 7 assigned items for the Linking engine, against 3 of 8 for the baseline, over 8 leave-one-item and 8 leave-one-group items, so one error moves any rate by a large step. False accepts, assignments of items that should be new, are counted in E7 only. The sensitivity check moves 39 weights by ±1 and ±2 bits one at a time: 20 of 33 pair links change verdict under a single-layer ±2 move, so their verdicts depend on the hand-set priors and are flagged as fragile. With platform ids hidden (E1) the Linking engine recovers 11 of 11 true campaign pairs: recall without the platform layer is 11 of 11, while precision is safe, with 0 of 37 must-not-link pairs violated. The baseline recovers 11 of 11 and violates 34 of 37.

Three caveats apply to the headline numbers. 4 of the 11 recovered pairs come from consolidating one incident's same-host captures into one site. The campaign links are sensitive to the assumed weights: 10 of 13 are fragile at ±2 bits (E6). 11 pairs is a regression suite, not a benchmark.
<!-- eval:limitations end -->

- **The weights are priors.** They are hand-set bits, not fitted to the data. Section 9 of [docs/methodology.md](docs/methodology.md) lists the limits. A learned table (HACK-23) can be passed with `ftf link --weights`, but the default run keeps the hand-set priors.
- **The demo base is a slice.** The walkthrough uses the hour-4 slice (`base-h4-main`), not the full challenge base, so the demo shows the method rather than the evaluation. The scaling figures in the methodology are design estimates and have not been measured.
- **Calibrated content views are off by default.** Text, image and face similarity add bits to the ledger only when a calibration is passed with `--content-views` (HACK-24). Without it they do not change any score.
- **The brand stop-list is a file.** It is on by default and reads `$FTF_BRAND_STOPLIST` or its default path on the workbench. The jury image ships the file (HACK-75). Where it is missing, the engine prints a warning and scores as if no stop-list existed.
- **Enrichment and live fetching depend on outside services.** When a service is down, the item carries less evidence and is more likely to go to review.
- **Campaigns rotate.** Operators change pages, pixels and ad accounts inside a campaign. The campaign relation is kept strict for that reason, so variants can end in review rather than in a campaign.
- **One distractor class is still partly failing.** The synthetic kit copies (E4 b) do not all pass. The failure is reported in the E4 table, not hidden.

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
| `src/ftf/features/` | content features: text, image, video, people and brands, faces (`docs/features/`) |
| `src/ftf/pipeline/` | end-to-end analysis (`ftf analyze`) and the export (`ftf export`); stage adapters in `stages.py` |
| `src/ftf/app/` | Streamlit demo app (`ftf serve`); evidence ledger in `ledger.py`, campaign views in `relations.py` |
| `src/ftf/contract/` | the data contract (pydantic models); see `docs/contract.md` |
| `docs/` | `contract.md`, `methodology.md`, `container.md` (the jury run), and the details of the fetcher, scanner and enrichment |
| `tests/` | pytest suite, run on the workbench VM |
| `scripts/vm.sh` | cloud dev loop: sync to the workbench VM, run tests or commands there |
| `scripts/check_no_private_data.py` | fails if personal data, secrets or raw challenge files reach tracked files or commit messages |
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

### Private-data check

`scripts/check_no_private_data.py` fails on three things only: personal data of private people (e-mail
addresses and phone numbers, including the contacts in the organiser dataset), secrets (private keys,
cloud and forge tokens, password or API key assignments), and raw challenge files committed as files
(HAR, MHTML, WARC, PDF, screenshots, HTML dumps, by extension or by content). Scam domains, URLs and
tracker, account, tenant and pixel ids, and the names of public figures and brands may be in git
(owner, 2026-11-22). The dataset exists only on the workbench VM, so the check runs there. Write the
commit messages on the laptop, copy them to the VM with `gcloud compute scp`, then run:

```bash
git log --all --format=%B > commit-messages.txt        # on the laptop
scripts/vm.sh run -- true                              # sync the worktree
gcloud compute scp commit-messages.txt ftf-workbench:/work/<slug>/ --project follow-the-fake \
  --zone europe-central2-a --tunnel-through-iap --quiet
scripts/vm.sh run -- python3 -I scripts/check_no_private_data.py \
  --dataset /data/derived/ingest/organiser/dataset.json --root . \
  --messages /work/<slug>/commit-messages.txt          # <slug> is the worktree directory name
```

A hit prints `file:line: class (digest)`, where class is `personal`, `secret` or `raw`, and a short hash
of the match, never the match itself. Exit 0 means no hit, and exit 1 means at least one. Files that
are invented test fixtures are listed by path in the script with their reason (`PERSONAL_ALLOWED`,
`RAW_ALLOWED`); reserved domains such as `example.com` and `.test` are placeholders and never hits.

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
Chromium needs (LGPL, MPL or GPL in part) are a named exception (HACK-68, owner, 2026-11-22), listed
in that document. The base image's own packages include GPL and LGPL ones and await an owner decision.

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
