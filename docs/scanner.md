The README summarises the sources and their terms. This page holds the details.

`ftf scan` searches public ad libraries by keyword and date, turns every ad it has not seen
before into one contract `Incident`, and appends it to a queue of items waiting for
assignment to a campaign. It never opens a landing page; landing URLs are only queued for the
sandboxed URL fetcher.

| Source | How | What an item carries |
|---|---|---|
| `meta` | Meta Ad Library search page in headless Chromium, **no login**; ads are read from the page's own GraphQL responses and the JSON embedded in the first page | Library id, page id and name, ad text, title, display domain, landing URL as declared to Meta, creative image links, creative group (`collation_id`), platforms, start and end date, taken-down flag |
| `google_atc` | Google Ads Transparency Center RPC endpoints, plain HTTPS, no key; keywords match advertiser names and domains (the Center has no ad-copy search), region Poland | Creative id, advertiser id and name, advertiser domain, creative image or preview link, format, first and last shown |
| `google_bq` | Google's public BigQuery dataset `bigquery-public-data.google_ads_transparency_center.removed_creative_stats`: creatives Google **removed** in Poland since `--since` for scam-like reasons (misrepresentation, account suspension, dishonest behaviour, financial products, abusing the ad network). Google's sanctioned bulk route: no scraping, no rate limit. Needs a GCP project to bill (`--bq-project` or `FTF_BQ_PROJECT`) and a token (`FTF_BQ_TOKEN` or `gcloud auth print-access-token`); otherwise it reports `unavailable`. One run scans about 3.4 GB (about 0.02 USD on demand, inside the free monthly terabyte); `maximumBytesBilled` caps it at 10 GB | Creative id, advertiser id when the page URL names it, first and last shown in Poland, removal reason. No ad text, image or landing URL in the dataset. It shares the id `adlib:<CR…>` and the deduplication key with `google_atc` |
| `certpl` | CERT Polska warning list (`hole.cert.pl/domains/v2/domains.json`), public and keyless | Register id, newly listed domain, listing date |

```bash
ftf scan --keywords-file keywords.txt                 # all sources, last 7 days
ftf scan --source meta --keywords "term one,term two" --since 2026-11-14
ftf scan --source certpl --cert-filter invest,inwest  # only CERT domains containing a filter word
ftf scan --source google_atc --keywords-file keywords.txt --max-ads 50 --delay 3 --out scan
ftf scan --source google_bq --bq-project my-gcp-project --since 2026-10-22   # run on the workbench
```

- **Search terms.** `meta` and `google_atc` need terms: `--keywords` gives one comma-separated
  list to both, `--keywords-file` one list per source (the two are exclusive). Without terms the
  run stops with exit 2; `certpl` and `google_bq` need none. There are no built-in terms.
  The file has a `[meta]` and/or `[google_atc]` heading, then one term per line; `#` starts a
  comment line. `config/scanner-keywords.example.txt` shows the format with invented terms.
  The list in use is private: it lives in `gs://follow-the-fake-data/config/scanner-keywords.txt`
  and the sandbox reads a copy at `/opt/hack-20/config/scanner-keywords.txt`. Real terms never go
  into git, a PR, a tracker or a doc.

- **Output** (`--out`, default `./scan`), in the same per-capture folder layout as the URL
  fetcher, so one ingestion adapter reads both. One folder per new ad,
  `<capture id>/` with capture id `<run stamp>-<source>-<ad id>`, holding `ad.json` (the
  library fields: source, ad id, page and advertiser, text, landing URL and domain, creative
  URLs, first and last shown), `capture.json` (manifest with a contract `Incident`) and the
  downloaded creatives (`creative-<n>.<ext>`). Ids never depend on time: an ad is
  `adlib:<ad archive id>` (Meta: the Ad Library archive id; Google: the creative id `CR…`,
  which is unique across advertisers) with capture `adlib:<id>:ad`; a CERT Polska entry is a
  bare domain, `certpl:<registrable domain>` with capture `certpl:<domain>:listing` (one per
  registrable domain, its newest listing). `ad.json` carries `source`, `stable_id`,
  `ad_archive_id` and `kind` (`ad` or `domain`). The folder and `ad.json` are the stable output;
  the Incident is provisional, and the ingestion adapter builds the canonical one. Next to the
  folders: `dataset.json` (a contract `Dataset` of every incident), `queue.jsonl` (one line per
  new ad: key, source, ad id, capture folder, incident id, landing URL and domain, run id,
  status `pending`, `assigned` or `rejected`), `state/seen.json` (every key ever queued) and
  `runs/<run id>/summary.json` (per source and query: status `ok`, `empty`, `throttled`,
  `blocked`, `error`, `skipped` or `unavailable`, counts and evidence such as HTTP status,
  final URL and GraphQL calls seen).
- **Library**: `ftf.scanner.scan(sources, keywords, since, out=…)` runs the same scan and
  returns the new ads (`ScannedAd`: key, capture folder, `ad.json` fields, provisional
  Incident); `ftf.scanner.pending(out)` lists the queue items waiting for assignment and
  `ftf.scanner.mark(out, keys, "assigned")` updates them.
- **Deduplication**: by source plus the library's own id, across runs. A run saves each source
  as soon as it finishes: capture folders, then the queue, then `seen.json`; keys already in the queue also count as seen,
  so a run that dies half-way never queues an ad twice. A lock file stops two runs at once.
- **Indicators** per ad (all `static`): `advertiser_page_id`, `advertiser_page_name`,
  `advertiser_id`, `advertiser_name` (distribution account); `ad_text`, `ad_title`,
  `ad_collation_id`, `ad_display_domain`, `creative_url` (creative); `landing_url`,
  `landing_domain` (landing). The search term is kept as `scan_keyword` in the collector layer,
  so it never links two ads.
- **Dates**: `--since` filters by last shown (Google, BigQuery) or listing date (CERT
  Polska); for Meta it is passed as the Ad Library start-date filter, so ads that started
  earlier and still run are not found.
- **Politeness**: a few keywords per run, one request at a time with `--delay` seconds between
  requests (default 5), a hard cap of `--max-ads` ads per source per run (default 150) and
  `--max-per-keyword` (default 50). The browser loads no images, media or fonts itself.
- **Creatives** are hostile data. With `--download-creatives` (used on the sandbox only),
  images are fetched only over HTTPS from the platforms' image CDNs, only `image/*` up to
  5 MB, at most 2 per ad and `--max-creatives` (default 100) per run, and stored under their
  SHA-256 without being opened. Meta's signed image links expire
  within days, so they are fetched in the run that finds the ad.

**Meta's terms.** Meta's terms (Automated Data Collection Terms, `facebook.com/robots.txt`)
forbid collecting data from Facebook by automated means without written permission. The `meta`
source does exactly that, from a logged-out headless browser, at low volume, from a sandbox
VM. It is a demonstration of the method for this challenge, not a production setup. The ads
it reads are the ones Meta publishes for transparency under Article 39 of the EU Digital
Services Act. **The production route is the official Meta Ad Library API** (`/ads_archive`,
all ads delivered in the EU, `ad_reached_countries=PL`), which needs a Meta developer account
with identity confirmation; the `meta` source should be replaced by an API client there. The
other two sources do not depend on it: Google's Transparency Center and the CERT Polska list
are public and keyless.

**In the jury's container** there is no scheduler; run the scanner by hand and mount a
directory for its output:

```bash
docker run --rm -v "$PWD/scan:/home/ftf/scan" ftf scan --source google_atc --source certpl --out scan
```

The `meta` source needs the optional `playwright` package and Chromium, which the image does
not include. Without them it reports `unavailable` and the other sources still run. To use
it anyway: `pip install '.[fetch]' && playwright install --with-deps chromium`.

**Scheduled runs (team setup).** The scanner runs only on the sandbox VM `ftf-sandbox`, never
on the laptop or the workbench. `scripts/scanner-sandbox.sh install` copies the code to
`/opt/hack-20` there (tracked files only), builds a venv with Chromium and installs a systemd
timer (`deploy/scanner/`) that runs `ftf scan --download-creatives --keywords-file /opt/hack-20/config/scanner-keywords.txt` as the unprivileged user
`hack20` 2 minutes after it starts and then every 6 hours while the VM is up. Each source is
saved as soon as it finishes, so a run stopped half-way keeps what it found.
`scripts/scanner-sandbox.sh status` shows the timer, the runs and the queue.
`scripts/scanner-sandbox.sh keywords` copies the keyword file from the bucket to the sandbox: it
runs on the workbench, which streams `gs://follow-the-fake-data/config/scanner-keywords.txt` to
the sandbox over the internal network with a key that can only write that file, so the laptop
just issues the command. Until the file exists the timer skips its runs (`ConditionPathExists`),
and `status` says whether it is there.
`scripts/scanner-sandbox.sh authorize-workbench` (once) and `scripts/scanner-sandbox.sh pull`
(a read-only mirror: queue statuses are changed with `ftf.scanner.mark` on the sandbox copy,
which a pull overwrites on the workbench) copy the output from the sandbox to the workbench (`/data/derived/scanner/`) over the internal
network, with a key restricted to one read-only command, and sync it to
`gs://follow-the-fake-data/outputs/scanner/`. Nothing passes through the laptop.

**Meta from a cloud IP (observed 2026-11-21).** From the sandbox VM (Google Cloud,
europe-central2) the Ad Library page loads without login after a JavaScript challenge (the
first response is HTTP 403, then the browser passes). The first page of results, about 30 ads
per keyword, comes with the page. The GraphQL calls behind scrolling return
`Rate limit exceeded` (code 1675004), so the scanner gets about 30 new ads per keyword per
run from Meta. It reports that query as `throttled`, keeps the first page and stops
scrolling at once. More keywords per run, or the official API, are the way to more ads.

**Google from a cloud IP (observed 2026-11-21).** The first run from the sandbox got 59
creatives, then the Transparency Center answered HTTP 429 (too many requests); runs in the
following half hour got 429 on the first request. The scanner records the query as `blocked`
with the error, skips the remaining Google queries of that run and tries again only at the next
scheduled run. CERT Polska and Meta still delivered in those runs.
The `google_bq` source is the way around this: it reads Google's own published dataset instead
of the website. It runs on the workbench (the sandbox has no cloud credentials), so it is
not part of the sandbox timer; a manual run on 2026-11-21 queued 115 removed creatives
(`/data/derived/scanner-bq/`, `gs://follow-the-fake-data/outputs/scanner-bq/`).
