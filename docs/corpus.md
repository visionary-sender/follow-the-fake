# Background corpus (HACK-18)

A background set of Polish scam ads, scam landing pages and real pages of the outlets and brands
that scams impersonate. Linking uses it to measure how rare a feature is and to set similarity
baselines. Visual uses it as a background set for embeddings. Evaluation uses the real pages as
must-not-link distractors.

The data is private and never goes into git:

| Batch | Bucket (`gs://follow-the-fake-data/outputs/corpus/…`) | What |
|---|---|---|
| batch-0 | `batch-0/` | scanner ads (Meta, Google ATC), CERT Polska domains, their landing pages (default fetch, victim-profile re-fetch, re-fetches from other egress IPs) |
| batch-1 | `batch-1/` | must-not-link real pages (outlet home and article pages, banks, brands, public bodies, public figures) |
| batch-2 | `batch-2/` | ads first seen by the HACK-45 egress scans, not already in batch 0 |
| lists | `lists/` | hand-picked seed lists and the label lists used below |

## What to read: `ingested/`

**The canonical data is `<batch>/ingested/`.** It is the output of `ftf ingest` (HACK-9, with the
indicator extractors on main) over every folder of the batch, with these files:

- `dataset.json`: contract `Dataset`, ids from `ftf.ingest.ids`. Artefact paths are relative to the
  batch directory itself, so use `--root` / the resolve root `/data/derived/corpus/<batch>` (not
  `/data/derived/corpus`).
- `corpus_kind.jsonl`: one row per ingested incident id, with `corpus_kind`, `scam_label`,
  `corpus_flags`, `profiles` (which fetch profiles it holds), `fetch_folders` and `corpus_items`
  (every corpus item that points at it: source, query keyword, source ref).
- `summary.json`: indicators per layer, counts per `corpus_kind` and `scam_label`, and the
  effective sample.

`ftf ingest` merges every fetch of one requested URL in a run into one incident: the default
fetch is `:c1`, a victim-profile or egress re-fetch `:c2`, … with a `fetch_profile` collector
indicator. Scanner items are `adlib:<ad id>` (capture `:ad`) and `certpl:<registrable domain>`
(capture `:listing`).

### `corpus_kind`

| value | what it is |
|---|---|
| `ad_record` | an ad-library ad (Meta Ad Library, Google Ads Transparency Center) found by a scanner keyword query; creatives attached where the scanner downloaded them |
| `fetched_landing` | a fetched landing page where at least one fetch (default, victim profile or egress) loaded a page |
| `bare_domain` | a CERT Polska list entry, or a landing URL, with no live page: not fetched, dead, or cloaked for every visitor tried |
| `must_not_link` | a real page of an impersonated outlet, bank, brand, public body or public figure |
| `must_not_link_blocked` | a hand-picked real page the fetcher could not load (e.g. HTTP 403); honest data, not a distractor |
| `exclude` | an ingested incident the corpus does not vouch for: captures from the fetcher's failure window, a duplicate fetch of a covered URL. Drop these rows. |

### `scam_label`

Keyword search returns many legitimate advertisers (brokers, brands, namesakes of the person
keywords), so scanner items are **not** all scams. Each row has a rule-based label; it is a filter,
never ground truth. First matching rule wins:

1. `not_scam`: a hand-picked real page.
2. `listed`: a CERT Polska list entry, or an item whose landing domain (requested or final URL,
   any fetch) is on the CERT Polska list.
3. `legit_suspected`: the landing domain is on `lists/legit-domains.txt` (regulated brokers, real
   brands, and platform hosts such as facebook.com where the destination is unknown).
4. `suspected`: an ad found by a person's-name keyword (`lists/person-keywords.txt`) whose landing
   page was cloaked for the default visitor. A dead default fetch is `unknown`, even if the victim
   profile or another egress loaded the page (it may just have been down).
5. `unknown`: everything else. **Never treat `unknown` as scam.**

Use `listed` + `suspected` as the scam-side background, `not_scam` as must-not-link, and keep
`legit_suspected` / `unknown` out of anything that assumes a label.

### `corpus_flags`

- `wayback_infra`: an Internet Archive snapshot. Its rendered page and text are the outlet's,
  but its HAR, hosting and other infrastructure-layer indicators describe archive.org. Linking
  drops the infrastructure layer for these.

## Packaging files (per batch, for provenance)

`manifest.jsonl` (one row per corpus item: source, query, target URL, every fetch attempt with its
verdict: `fetch_*`, `victim_*`, `egress[]`), `summary.json` (counts, fetch outcomes, victim and
egress results) and `dataset.json` (the packager's own contract view, superseded by
`ingested/dataset.json`). The raw folders: `scanner/` (batch 0's pre-v1 scanner snapshot),
`scanner-v1/` (the same items in the scanner's v1 layout, `ftf corpus convert-scanner`),
`fetch-*` (fetcher runs: `fetch-1..3` default, `fetch-victim-*`, `fetch-egress-*`), `picked-*`
(must-not-link fetches) and `lists/`.

## How it is built

Live pages are fetched only on `ftf-sandbox`; everything else runs on the workbench and needs no
network.

```bash
# ftf-sandbox, as user ftf: fetch each target URL once per batch, concurrency <= 2
ftf corpus targets --queue scanner/queue.jsonl --run <scanner run> --out targets --parts 2
ftf fetch --list targets/urls-1.txt --out fetch-1
ftf fetch --profile victim-pl-mobile [--referer https://l.facebook.com/ --fbclid] \
    --list <cloaked URLs> --out fetch-victim-<n>          # second attempt for bot_wall/decoy/empty
ftf corpus targets --picked lists/must-not-link-home.tsv --out targets-home
ftf fetch --list targets-home/urls-1.txt --out picked-home
ftf corpus articles --picked lists/must-not-link-home.tsv --fetch picked-home --per-page 3 --out lists/articles.tsv
# workbench, after pulling the runs from the sandbox
ftf corpus convert-scanner --scanner scanner --out scanner-v1
ftf corpus build --root <batch> --scanner scanner --fetch fetch-1 --fetch fetch-2 --fetch fetch-3 \
    --victim-fetch fetch-victim-r1 --egress-fetch fetch-egress-1-meta ...     # -> manifest.jsonl
bash scripts/corpus-ingest.sh <batch>       # ftf ingest over all folders in ONE call + tag-ingested
```

`scripts/corpus-ingest.sh` is repeatable: re-run it when new extractors merge. It fails if fetched
pages yield 0 indicators.

## Sampling and biases

**Scam side (batch 0).** HACK-20 scanner runs on 2026-11-21 (ads started since 2026-11-14):

- **Meta Ad Library**: country PL, all ads, sorted by impressions, 4 keyword queries:
  "platforma inwestycyjna", "pasywny dochód", and two public figures' names. Meta throttled after
  30 ads per query and then answered HTTP 403, so the sample is the top 30 by impressions per
  keyword, not a random draw: 98 ads. HACK-45 repeated the queries from 6 other IPs and saw the
  same 98 ads (no new Meta ads).
- **Google Ads Transparency Center**: queries "trading" (50) and "invest" (9); HACK-45's egress
  scans added 26 more (batch-2). Google gives no landing URL, so the fetch target is the ad's
  display domain.
- **CERT Polska warning list**: the newest entries (150 per scanner run, 3 runs). These are bare
  domains, mostly delivery, marketplace and bank phishing, not celebrity investment scams.
- **Legitimate advertisers**: the keyword queries return regulated brokers, brands and namesakes of
  the person keywords. That is why `scam_label` exists.

**Fetches.** One Polish GCP IP, Chromium, no evasion, each URL once per profile. Bot walls,
decoys and empty pages got a second attempt with the victim profile (Polish phone; Meta items with
a Facebook referer and fbclid), and HACK-45 re-fetched the cloaked landers from other EU egress
IPs. Results per attempt are in `summary.json` (`fetch_outcomes`, `victim_profile`, `egress`).
Over half of the CERT Polska domains were already dead (many resolve to a non-public sinkhole
address, which the fetcher's guard refuses). The `fb.me` links in Meta ads lead to facebook.com
for every visitor tried, so their real destination is unknown.

**How this differs from NASK's cases.** NASK's cases are user-reported **celebrity-likeness
investment scams** that arrive on Instagram or Facebook through a cloaker, with fake news
articles cloning national outlets and AI-trading platform brands. The corpus is broader and
noisier: keyword ads include legitimate advertisers; most CERT entries are other scam types; and
live pages are what a cloaker shows to a datacenter or emulated visitor, often the decoy. So rarity
estimates from it are **conservative for kit and tracker features** (few real scam landers) and
**realistic for infrastructure features** (hosting, registrars, TLDs of fresh scam domains).

**Must-not-link side (batch 1).** Hand-picked, not sampled: real home, section and article pages
of the national news outlets, banks, delivery and marketplace brands and public bodies that scams
impersonate, plus encyclopedia pages of the public figures in the scanner keywords. Articles are
links taken from those home pages on the day of collection. One outlet answered HTTP 403 to the
sandbox for every URL tried (each once, no evasion); those captures are `must_not_link_blocked`,
and that outlet's real articles come from Internet Archive snapshots (`source = wayback`,
`source_ref` = original URL, flag `wayback_infra`).
