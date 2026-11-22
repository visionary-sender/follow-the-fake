# Text features (`ftf.features.text`)

Turns the words of an ad and its landing page into indicators. It exports raw similarity inputs
(texts, MinHash signatures, vectors). Thresholds and evidence weights belong to the linking
engine.

Run it on its own or as part of the pipeline:

```
ftf features --dataset IN.json --raw-root /data/raw --out OUT --families text
```

From code, call `ftf.features.pipeline.extract(incident, raw_root, out_dir)`. For one still image
(for example a video keyframe), call `ftf.features.text.features_for_image(path)`.

## Indicators

All indicators use layer `creative` and level `campaign`. Each derived indicator points back to
the text it came from with `provenance.locator = "src=<indicator id>"`.

| kind | value | source |
|---|---|---|
| `ocr_text` | the kept OCR lines, joined by newlines | every `ad_screenshot` / `screenshot` artefact |
| `page_text` | visible text, one line per block | the capture's main page: saved HTML, else MHTML, else HAR (`observed=live`) |
| `text_template` | hex MinHash, 128 × uint32 big-endian, over word 5-shingles of the **name-masked** text | each source text of at least 20 characters |
| `text_embedding` | vector, model `intfloat/multilingual-e5-small`, **384-d**, L2-normalised | same |
| `person_mention` | a gazetteer name in canonical form (note `gazetteer`), or a first-name + surname pair as written (note `name pair, surface form`) | same |
| `keyword` | a canonical keyword from a fixed scam lexicon | same |
| `topic` (**new kind**) | `investment`, `crypto` or `health` | same, only when one topic clearly dominates |
| `ocr_skipped` (layer `collector`, level `capture`) | `timeout` | the capture whose screenshot OCR hit `FTF_OCR_TIMEOUT`; never linked |

Sidecar files in `OUT/text/`:

- `masked.json` maps each source indicator id to `{capture_id, artefact_id, masked_text}`.
- `stats.json` holds the run's timings and errors.

To compare two `text_template` values, count the equal positions. Use
`ftf.features.text.template.estimate(a, b)`; it estimates the Jaccard of the masked shingle
sets, with a standard error of about 0.044.

## Method

- **OCR.** Tesseract 5 (`pol+eng`, `--psm 3`) runs as a subprocess. Its input is a greyscale
  copy of the image, upscaled to at least 1400 px wide (at most 3×). Scaling large screenshots down to
  2000 px was measured and dropped: it removed OCR text on two of six inputs (−1.5% and −9% characters). Each Tesseract call may run for
  `FTF_OCR_TIMEOUT` seconds (default 600; a value that is not a positive integer falls back to 600; the
  faces fallback OCR uses the same setting, previously 120 s). When it runs out, that image's OCR is skipped, one
  warning is logged per run, and its capture gets an `ocr_skipped` indicator. Page text, templates,
  embeddings and the other families still run. The ffmpeg frame call timeout is
  `FTF_VIDEO_TIMEOUT` seconds (default 900); ffprobe keeps 60 s. In a video keyframe, a timed-out OCR is
  logged and that keyframe is skipped (no indicator). A line is kept when its mean
  word confidence is at least 60. `ocr_text` keeps platform chrome such as "Sponsorowane" or
  "Identyfikator biblioteki". Lines made only of chrome phrases and OCR debris are removed (`ocr.content_lines`) from the text that
  feeds templates, embeddings and keywords.
- **Visible page text.** BeautifulSoup and lxml parse the page as inert markup; nothing is
  executed or rendered. The parser drops `script`, `style`, `noscript`, `template`, `svg`,
  `iframe`, `object`, `embed`, `canvas` and `head`. It also drops elements that are `hidden`,
  `aria-hidden="true"` or `<input type=hidden>`, and those with inline `display:none` or
  `visibility:hidden`. Comments are dropped too.
  - MHTML: the first `text/html` part.
  - Every saved HTML or MHTML page of a capture gives a `page_text` (pre-lander and lander).
    The HAR is used only when a capture has neither, taking the `text/html` response body with
    the most visible text.
  - Size caps: only the first 64 MB of HTML or MHTML is read; HARs over 64 MB are skipped.
  - Ingestion (HACK-9) owns these formats. This module keeps to what text extraction needs.
- **Masking** (`mask.py`) replaces, in this order:
  1. Emails become `<URL>`.
  2. Names of real news outlets and programmes (a fixed public list in `mask.py`) become
     `<OUTLET>`. This runs before URL masking, so "Onet.pl" and "Onet" mask alike.
  3. URLs become `<URL>`.
  4. Gazetteer people become `<PERSON>`. The match is on surname stems plus Polish case endings,
     so "Roberta Lewandowskiego" (and "ROBERT LEWANDOWSKI") matches. A surname alone counts as a person only when no other
     gazetteer entry shares its stem.
  5. Prices, dates and times, and then any other number, become `<PRICE>`, `<DATE>` and `<NUM>`.
  6. Every remaining pair of capitalised words becomes `<PERSON>`. This catches invented
     commenter names and invented platform brand names. A pair that starts with a
     common Polish first name is also reported as a `person_mention`.

  The gazetteer is a plain list in `ftf.features.people` (shared with faces, HACK-15).
- **Template fingerprint.** Texts with fewer than 5 masked tokens get no template, because they
  would all look alike. Otherwise it is MinHash with 128 permutations over lower-cased word 5-shingles of the
  masked text. Hashing uses blake2b, then fixed-seed `(a·x + b) mod (2^61 − 1)`. The output is
  deterministic across runs and machines.
- **Keywords and topic.** A fixed Polish/English lexicon of about 24 stems (investment, crypto,
  health and hook terms). A fixed lexicon keeps one incident's output independent of the batch
  around it.
- **Embeddings.** The ONNX export of `intfloat/multilingual-e5-small` runs on onnxruntime CPU
  (no torch). Input is `"passage: " + text`, truncated to 512 tokens. Output is the mask-weighted
  mean pooling, L2-normalised. Model files download on first use into `FTF_MODEL_DIR/hf` at a
  pinned Hub revision. If the model cannot load (missing extra, or no network on first use), the
  family logs one warning and skips embeddings; the other indicators are still written.
  - `FTF_TEXT_MODEL=e5-base` switches to `intfloat/multilingual-e5-base` (768-d, MIT, 1.11 GB).
  - The default is e5-small: it is 2.4× smaller and fast enough on a jury laptop.
- **Offline.** `ftf features prefetch` downloads the ONNX graph and tokenizer into the same cache.
  With `FTF_OFFLINE=1` the loader passes `local_files_only`, so it only reads that cache, and
  `ftf features` stops before it starts if a file is missing, naming it.
- **Threads.** `FTF_THREADS` (default `min(4, cpu_count)`) caps onnxruntime and Tesseract.

## Models and packages

| item | licence | download / size |
|---|---|---|
| `intfloat/multilingual-e5-small` (revision `614241f6`), `onnx/model.onnx` + `onnx/tokenizer.json` | MIT | 470 MB + 17 MB |
| Tesseract 5 with `pol`, `eng` traineddata (system package, already in the VM image) | Apache-2.0 | ~15 MB + ~4 MB (tessdata_fast) |
| onnxruntime | MIT | ~15 MB wheel |
| tokenizers | Apache-2.0 | ~3 MB wheel |
| huggingface_hub | Apache-2.0 | small |
| beautifulsoup4 | MIT | small |
| lxml | BSD-3-Clause | ~5 MB wheel |
| Pillow | MIT-CMU (HPND) | ~4 MB wheel |

The final container needs `tesseract-ocr`, `tesseract-ocr-pol` and `tesseract-ocr-eng` from apt.
Without them, OCR is skipped with one warning.

## Measured on `ftf-visual`

CPU time per item, as process CPU plus Tesseract child CPU, with 4 threads. The run was Ingestion's
organiser dataset (`gs://follow-the-fake-data/outputs/ingest/organiser/`): 25 incidents, 37
screenshots and 28 page captures. The e2-standard-8 VM was shared with other workers.

| step | items | CPU s / item (mean) |
|---|---|---|
| OCR (Tesseract, upscale included) | 37 | 3.2–4.5 (varies with the VM's other load) |
| visible page text | 28 | 0.05 |
| e5-small embedding | 63 | 0.35–0.48 |
| whole run (wall clock) | | 68–97 s |

Output counts: `ocr_text` 36 (one of 37 images had no confident text), `page_text` 28,
`text_template` 63, `text_embedding` 64, `person_mention` 230, `keyword` 174, `topic` 25
(investment 19, health 6). No errors.

Outputs are in `gs://follow-the-fake-data/outputs/visual/hack-13/organiser/`, with the pair reports
in `text/report-*-p3p4.json`.

### OCR time on 2 cores (HACK-89)

`ftf features` (all families) per input, `taskset -c 0,1`, `OMP_NUM_THREADS=2`, `FTF_THREADS=2`,
`ftf-visual`. The inputs are the three incidents with the most OCR-able images in each set (the
corpus maximum is 5 images, so these are not large).

| input | images | before (s) | after, run with the 2000 px downscale (s) |
|---|---|---|---|
| organiser, rank 0 | 11 | 263.3 | 207.3 |
| organiser, rank 1 | 6 | 31.7 | 26.9 |
| organiser, rank 2 | 3 | 20.3 | 22.9 |
| corpus, rank 0 | 5 | 38.2 | 73.9 |
| corpus, rank 1 | 4 | 9.4 | 14.2 |
| corpus, rank 2 | 4 | 17.2 | 16.2 |

The downscale was then dropped (see Method): it removed OCR characters on two inputs (organiser
rank 0: 9017 → 8879; corpus rank 2: 653 → 592), so the kept code takes the "before" OCR path. The
corpus rank 0 rise is not explained by OCR (its text is unchanged, 4354 → 4356 characters), so it is
most likely load on the shared VM; treat single timings here as ±50 %. The timeout change itself
(`FTF_OCR_TIMEOUT`, default 600 instead of 180) applies per Tesseract call. I did not record per-image
times, so I cannot say whether any single call on these inputs passed 180 s. Timing of the final code
(without the downscale) was not re-measured before the PR.

### Acceptance

- **A screenshot-only input yields its ad text.** 36 of the 37 screenshots give an `ocr_text`.
  On ad screenshots that holds the advertiser name, the headline and the call to action; Ad
  Library chrome is kept in `ocr_text` and filtered from the derived text. The test
  `test_ocr_screenshot_yields_text` checks this on a synthetic screenshot generated in the test.
- **BACKUP p3/p4 (the same article with a different celebrity) is one template with different
  names.** Each page gives a different gazetteer `person_mention` for the impersonated person,
  while the masked texts share most shingles.

  The p3↔p4 pair compared with the other 377 `page_text` pairs (the latest run reads every saved
  page of a capture, so one more page and one rank lower than the table):

  | measure | p3↔p4 | other pairs: median | p95 | max | rank of p3↔p4 |
  |---|---|---|---|---|---|
  | raw word-5-shingle Jaccard | 0.552 | 0.000 | 0.003 | 0.988 | 13 |
  | masked word-5-shingle Jaccard | **0.629** | 0.000 | 0.004 | 1.000 | 13 |
  | `text_template` MinHash estimate | **0.680** | 0.000 | 0.000 | 1.000 | 13 |
  | e5-small cosine | 0.952 | 0.837 | 0.912 | 1.000 | 12 |

  The 12 pairs ranked above p3↔p4 are all exact or near copies of one page (cases
  do_udostepnienia 1, 2 and BACKUP p1, p2, p7, p15), at about 0.99. Every unrelated pair is at or below the p95 of
  0.004, so p3↔p4 sits about 150× above that.

  Masking moves the pair from 0.55 to 0.63, which matches the 0.65 measured earlier in the
  knowledge base. It cannot reach 1.0, because p4 is not a pure name swap: several paragraphs
  were rewritten.

## Known limits

- **Short ad texts.** An ad screenshot often holds fewer than 5 content words after the chrome is
  removed, so its 5-shingle set is a single shingle. Masked Jaccard and the template are then
  close to all-or-nothing: the p3/p4 ad pair scores 0.0, while its embedding cosine is 0.89.
  Linking should rely on the embedding (and the image family) for short `ocr_text`.
- **Embedding scale.** e5 cosines sit high for any two Polish scam pages: the median unrelated
  pair is 0.84. Calibrate them against other scam pages, as `07-method-theory.md` §1.6 says.
- **Two-column screenshots.** Tesseract may interleave lines from two columns (seen on the Ad
  Library detail view), and small UI text is sometimes garbled.
- **Person matching is heuristic.**
  - The gazetteer covers about 45 people.
  - "Name pair" mentions include invented commenters, which is useful as a template signal.
  - A capitalised pair that is a brand name is masked as `<PERSON>`, but it is not reported unless its first word is a common first name.
- **Not read yet.** `text` artefacts (ad copy pasted as text) give no indicators.
- **Title Case over-masking.** Any capitalised word pair becomes `<PERSON>`, so Title Case
  headlines and institution names ("Narodowy Bank Polski") collapse too. That trades some
  over-masking for catching invented commenter and brand names.
- **HAR main document.** The longest HTML body may be a consent wall or a cloaking decoy;
  Ingestion's main-document choice should replace it.
- **No language detection.** Masking and the lexicon assume Polish; other languages still get
  OCR (eng), templates and embeddings.
