# Background corpus (HACK-18)

A background set of Polish scam ads, scam landing pages and real pages of the outlets and brands
that scams impersonate. Linking uses it to measure how rare a feature is and to set similarity
baselines. Visual uses it as a background set for embeddings. Evaluation uses the real pages as
must-not-link distractors.

The data is private and never goes into git:

| Batch | Bucket | What |
|---|---|---|
| batch-0 | `gs://follow-the-fake-data/outputs/corpus/batch-0/` | scanner ads (Meta, Google ATC), CERT Polska domains, their fetched landing pages |
| batch-1 | `gs://follow-the-fake-data/outputs/corpus/batch-1/` | must-not-link real pages (outlet home pages, articles, banks, brands, public figures) |

## Format

Each batch directory holds:

- `dataset.json`: a contract `Dataset` (v0.1.0) with incidents only.
- `manifest.jsonl`: one flat row per incident, keyed by `incident_id`. Rows hold `corpus_kind`,
  `source`, `query`, `source_label`, `target_url`, `fetch_capture_id`, `fetch_outcome`,
  `fetch_category`, `final_url`, `has_landing_page`, `lander_duplicate_of`, `bytes`. Must-not-link
  rows also hold `brand`, `page_kind` and `capture_ids`.
- `summary.json`: counts per `corpus_kind` and source, fetch outcomes and total bytes.
- the files themselves. `scanner/` is a snapshot of the HACK-20 scanner output (`queue.jsonl`,
  `runs/*/incidents-*.json`, `creatives/*.jpg`). `fetch-*/` and `picked-*/` are `ftf fetch` output
  (HACK-17 layout: `<capture_id>/{url.txt,page.har,page.html,screenshot.png,capture.json}`).
  Artefact paths in `dataset.json` are relative to the batch directory.

### The four kinds of items

Visual and Linking use these differently, so every capture carries a `corpus_kind` indicator.
The manifest has the same value.

| `corpus_kind` | What it is | Ids | Files |
|---|---|---|---|
| `ad_record` | an ad-library ad (Meta Ad Library, Google Ads Transparency Center) found by a scanner keyword query | incident `adlib:<ad id>`, capture `adlib:<id>:ad`, plus `adlib:<id>:lander` when its landing URL was fetched | creatives (`ad_screenshot`) where the scanner downloaded them; the lander's HAR/HTML/screenshot |
| `fetched_landing` | a landing page that the fetcher loaded (`category = live`) | an ad's `adlib:<id>:lander` capture, or the fetcher's own incident `fetch:<capture_id>` (capture `fetch:<capture_id>:c1`) for CERT Polska domains | `page.har`, `page.html`, `screenshot.png`, `url.txt` |
| `bare_domain` | a CERT Polska list entry with no live page: not fetched, dead, or cloaked; also an ad lander that was cloaked or dead | incident `certpl:<register id>`, capture `certpl:<id>:c1` | none; the domain is a `landing_domain` indicator, and the fetch verdict is in the manifest |
| `must_not_link` | a real page of an impersonated outlet, bank, brand or public figure | the fetcher's own incident `fetch:<capture_id>` | as for `fetched_landing` |

A fifth value, `must_not_link_blocked`, marks a hand-picked real page that the fetcher could not
load (e.g. an outlet that answers HTTP 403). It is kept as honest data, but it is not a distractor, because
its capture shows an error page.

**Victim-profile re-fetch.** Every batch-0 URL whose first verdict was `bot_wall`, `decoy` or
`empty` was fetched once more with HACK-17's `--profile victim-pl-mobile` (a Polish phone
browser). Meta items also got `--referer https://l.facebook.com/ --fbclid`. Both captures are kept.
The second one is `adlib:<id>:lander-victim` for ads and `<incident>:victim` otherwise. It is
tagged `corpus_profile = victim-pl-mobile` and has its own `corpus_kind` from its own verdict. The
manifest has `victim_capture_id`, `victim_outcome`, `victim_category`, `victim_final_url` and
`victim_flipped_to_live`. `summary.json` → `victim_profile` counts the default → victim verdict
pairs. A CERT domain that loads only under the victim profile counts as `fetched_landing`.

An ad record's incident-level kind stays `ad_record`. Its lander capture is tagged
`fetched_landing` if the page loaded, otherwise `bare_domain`.

Ids follow the cross-project scheme (HACK-9 `ftf.ingest.ids`, HACK-41). Fetched pages keep the
fetcher's ids unchanged. The `adlib:` lander capture is the fetcher capture re-keyed under the ad.
Its `fetch_capture_id` is in the manifest.

Several ads often share one landing URL (one advertiser runs many ad versions). Each URL is
fetched once per batch. The lander captures of all those ads point at the same files, and every
copy except the lexicographically smallest capture id sets `duplicate_of` to it. Bytes are counted
once.

Other indicators on each capture, all in the `collector` layer (they describe how the corpus was
gathered, so `linkable()` is false and they never count as link evidence):

- `corpus_source`: the scanner source and query keyword (`meta:Brzoska`, `google_atc:trading`,
  `certpl`), or `picked:<brand>:<page_kind>` for hand-picked pages;
- `corpus_brand` and `corpus_page_kind` (must-not-link only);
- `fetch_outcome` (from the fetcher).

`Incident.source` is the scanner's (`scanner_meta`, `scanner_google_atc`, `scanner_certpl`) or
`hand_picked_must_not_link`. `source_ref` is the ad id, the CERT register id or the page URL.

No contract change was needed. `corpus_kind` is a free snake_case indicator kind, which the
contract allows as a patch-level addition.

## How it is built

Everything runs in the cloud. Live pages are fetched only on `ftf-sandbox`. Packaging runs there
or on the workbench, and needs no network.

```bash
# on ftf-sandbox, as user ftf
ftf corpus targets --queue scanner/queue.jsonl --run <scanner run> --out targets --parts 2
ftf fetch --list targets/urls-1.txt --out fetch-1     # and urls-2 -> fetch-2; concurrency <= 2
ftf corpus targets --picked lists/must-not-link-home.tsv --out targets-home
ftf fetch --list targets-home/urls-1.txt --out picked-home
ftf corpus articles --picked must-not-link-home.tsv --fetch picked-home --per-page 3 --out articles.tsv
ftf fetch --list <targets of articles.tsv> --out picked-articles
# on the workbench, after pulling the run from the sandbox
ftf corpus build --root <batch dir> --scanner scanner --run <run> --fetch fetch-1 --fetch fetch-2
ftf corpus build --root <batch dir> --picked lists/home.tsv --picked lists/articles.tsv \
    --picked-fetch picked-home --picked-fetch picked-articles
```

- `ftf corpus targets` turns scanner queue rows into URLs. It uses the ad's landing URL, else
  `https://<landing domain>/`. URLs are deduplicated and interleaved by source, so every source
  starts early.
- `ftf corpus articles` takes real article links from a fetched home page. It parses the HTML as
  inert text, keeps only links on the same registrable domain, and matches common article URL shapes.
- `ftf corpus build` writes `dataset.json`, `manifest.jsonl` and `summary.json`. The result
  validates against the contract.

The hand-picked seed lists live in the bucket, not in git: `gs://follow-the-fake-data/outputs/corpus/lists/`
(each batch also has its own copy under `lists/`).

## Sampling and biases

**Scam side (batch 0).** This is the HACK-20 scanner's first run (`20261121T212630Z`, 2026-11-21,
ads started since 2026-11-14) plus the CERT Polska entries from its later runs.

- **Meta Ad Library**: country PL, all ads, sorted by impressions, 4 keyword queries:
  "platforma inwestycyjna", "pasywny dochód", "Brzoska", "Glapiński". Meta throttled after 30 ads
  per query ("Rate limit exceeded"). The next scanner run got HTTP 403 for every query. So the
  sample is the top 30 by impressions per keyword, not a random draw: 98 ads in total
  (Glapiński returned only 8).
- **Google Ads Transparency Center**: queries "trading" (50) and "invest" (9). Google gives no
  landing URL, so the fetch target is the ad's display domain. All 50 "trading" ads share one
  destination, which answered with a bot wall. Only the 9 "invest" domains loaded.
- **CERT Polska warning list**: the newest entries per run (150 per run). These are bare
  domains, mostly delivery, marketplace and bank phishing (`olx.pl-…sbs`, `allegrolokalnie…top`,
  `vinted…`), not celebrity investment scams.
- The keyword queries also return **legitimate advertisers**: real brokers for
  "platforma inwestycyjna", and the named person's own company for a person's name. Scanner ads are not all scams. Treat
  them as an unlabelled background, which is what a rarity baseline needs. Don't use them as
  positives.

**Dead and cloaked rates** (fetches from a Polish GCP IP, desktop Chromium, no evasion; each URL
fetched once):

| Source | Fetched | live | cloaked | dead / HTTP error |
|---|---|---|---|---|
| Meta (unique landing URLs) | 88 ads | 23 | 57 (39 decoy: `fb.me` links that redirect to facebook.com; 18 bot walls) | 8 |
| Google ATC | 59 ads | 9 | 50 (bot wall) | 0 |
| CERT Polska (run 1) | 150 | 28 | 48 (21 bot wall, 19 empty, 8 decoy) | 74 (58 resolve to a non-public sinkhole address and are refused by the fetcher's guard) |

10 Meta ads had no landing URL, so they were not fetched.

**How this differs from NASK's cases.** NASK's cases are user-reported **celebrity-likeness
investment scams**. They arrive on Instagram or Facebook through a cloaker, with fake news
articles that clone national news outlets, and AI-trading platform brands. The corpus is broader
and noisier:

- the ads come from keyword search, so they include legitimate advertisers;
- most CERT entries are other scam types;
- live pages are what a cloaker shows to a datacenter IP with no click ID. That is often the
  decoy, not the scam lander (the 39 `fb.me` decoys and the many empty pages).

So rarity estimates from this corpus are **conservative for kit and tracker features** (few real
scam landers) and **realistic for infrastructure features** (hosting, registrars, TLDs of fresh
scam domains).

**Must-not-link side (batch 1).** Hand-picked, not sampled: real home, section and article
pages of the national news outlets, banks, delivery and marketplace brands, and public bodies that
scams impersonate, plus encyclopedia pages of public figures named in the scanner keywords. The
articles are links taken from those home pages on the day of collection. The list is in the bucket.

One outlet answered **HTTP 403** to the sandbox for every URL tried: home, sections and three
articles, each tried once with no evasion. Those captures are kept as `must_not_link_blocked`.
Real articles of that outlet were fetched from **Internet Archive** snapshots instead
(`Incident.source = wayback`, `source_ref` = the original URL, indicator
`corpus_wayback_snapshot`). Their HAR shows archive.org infrastructure, not the outlet's, but the
rendered page and text are the outlet's. Linking must not use their network features as the
outlet's.
