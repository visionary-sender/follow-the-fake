# Image features (`ftf.features.image`)

Near-duplicate detection for ad creatives, page screenshots and page assets. Robust to the
kits' colour randomiser, re-encoding and resizing. Also gives image embeddings for looser
similarity. The family exports raw similarity inputs (hashes and vectors) and no thresholds:
the linking engine calibrates them.

## Indicators

Layer `creative`, level `campaign`, extractor `image@0.1.0`.

| kind | value | notes |
|---|---|---|
| `image_phash` | 64-bit hex (16 chars) | DCT hash on a hue-invariant luma channel |
| `image_dhash` | 64-bit hex (16 chars) | gradient hash on the same channel |
| `image_pdq` | 256-bit hex (64 chars) | PDQ, bit-exact with the reference implementation |
| `image_embedding` | vector, model `google/siglip2-base-patch16-224`, dim 768 | L2-normalised; cosine = dot product |

Provenance: `artefact_id` is the screenshot, asset or HAR file. For an image found in a HAR
response, `locator` is `har_entry=<n>` and `note` is the request URL. Those indicators are
observed `live`; images from files are observed `static`. If an embedding was computed on the
top crop of a tall screenshot, its `locator` has `crop=top:<px>px`.

`features_for_image(path)` and `features_for_bytes(data)` return the same features for one
still image, so the video family (HACK-16) can run them on keyframes.

## Method

**Inputs.** The family reads `ad_screenshot`, `screenshot` and `asset` artefacts and image
responses in `har` artefacts. It does not trust file suffixes, because saved assets often end
in `.pobrane` or have no suffix at all. Any file that Pillow decodes as PNG, JPEG, GIF, WebP or
BMP is used; everything else is skipped without decoding it. HAR files are parsed as JSON, and
only base64 bodies with an `image/*` MIME type other than SVG are decoded. If the same bytes
appear twice in one capture (for example the saved file and its HAR response), they are
emitted once.

**Skip rules.** These are input rules, not similarity thresholds.
- SVG and any other non-raster format: never decoded or rasterised, since SVG is active markup.
- Icons and tracking pixels: shorter side below 40 px (the earlier prototype used the same rule).
- Decompression bombs: more than 120 Mpx, checked from the header before decoding.
- Flat images (standard deviation of a 64×64 luma thumbnail below 2): a blank frame hashes
  to a constant that would match every other blank frame.
- Animated GIF and WebP: first frame only. Transparency is composited on white.

**Hue invariance.** Every platform-A page sets `filter: hue-rotate(Ndeg)` with a random N
between 2 and 8 (signal report §5), so screenshots of the same creative differ in colour.
The CSS hue-rotate matrix keeps `0.213 R + 0.715 G + 0.072 B` unchanged; only 8-bit clipping
changes it. pHash and dHash are computed on that exact channel, so any hue rotation leaves
the hashes unchanged up to clipping. PDQ keeps its reference BT.601 luma, so that its hashes
stay compatible with other PDQ tools. A further colour-invariant variant was not added: the
CSS luma already gives 0–2 bits of change (table below).

**PDQ.** This is a numpy port of Meta's ThreatExchange `pdqhashing.cpp` (BSD licence): two
passes of a Jarosz box filter, 64×64 decimation, a 16×16 DCT, and the median as the bit
threshold. It needs no compiler in the container. A test checks that its output is
bit-identical to the `pdqhash` package (MIT, C++ binding) when that package is installed.

**Embeddings.** `google/siglip2-base-patch16-224` (SigLIP 2, Apache-2.0) via `transformers`
on CPU, with the vision tower's pooled output. A tall page screenshot (height more than
2× width) is cropped to its top 2× width before the model resizes it to 224×224. That crop is
the first screen, with the hero image and the headline. Weights download on first use into
`model_dir()/hf` (`FTF_MODEL_DIR`). If torch or transformers is missing, or the download
fails, the family still emits all hashes and logs a warning. `FTF_IMAGE_EMBED=0` turns
embeddings off and `FTF_THREADS` caps torch threads (default 4).

## Models and packages

| what | id / version | licence | download |
|---|---|---|---|
| SigLIP 2 base | `google/siglip2-base-patch16-224` (Hugging Face) | Apache-2.0 | 1.50 GB (HF cache: safetensors with both towers, tokenizer, config) |
| transformers | ≥4.50 (tested 5.24.0) | Apache-2.0 | |
| torch | ≥2.4 (tested 2.15.0+cpu; install the CPU wheel) | BSD-3-Clause | |
| ImageHash | ≥4.3 (tested 4.3.2) | BSD-2-Clause | |
| Pillow | ≥10 (tested 13.0.0) | MIT-CMU (HPND) | |
| PDQ algorithm | ported from facebook/ThreatExchange `pdq/cpp` | BSD | none |
| `pdqhash` (tests only, optional) | 0.2.8 | MIT | |

These are in the `features` extra, not in `LICENSES-THIRD-PARTY.md`.

## CPU time (ftf-visual, e2-standard-8 shared with other workers, 4 threads)

| step | per image |
|---|---|
| decode | 0.035 s |
| pHash + dHash + PDQ | 0.107 s (CPU) |
| SigLIP 2 embedding | 0.18 s wall, 1.08 s CPU summed over 4 threads |

COVERAGE_PLACEHOLDER

## Evaluation (acceptance)

Script: `scripts/image_eval.py`. The labelled pair list names organiser files, so it lives in
the bucket (`outputs/visual/hack-14/labels/pairs.json`). The full report is at
`outputs/visual/hack-14/eval/report.json`.

**Positives** are the near-duplicate groups from the signal report §7. Each group lists two
sides, and every image on side A is paired with its closest image on side B by PDQ (the report
names the groups, not the one-to-one mapping). Groups:

- case 14 ↔ p10 comment avatars (4 pairs; the p10 side is decoded from the p10 HAR)
- cases 2, 3 ↔ p5, 61 bank screenshot (2 pairs; different resolutions)
- case 48 ↔ p5 photo (1 pair)
- case 47 ↔ p10 product image (1 pair; HAR ↔ saved file)
- p3 ↔ p4 template images, paired by file name (6 pairs)

That is 14 pairs, and in only one of them the two files have the same bytes.

**Relabelled:** p3 and p4 also share the template's hero image name, but each case
re-generated that scene with its own celebrity: same composition, different picture. Hashes
correctly see two different images (pHash 28, dHash 20, PDQ 128). The embedding sees the
shared template (cosine 0.91). The pair is reported as a "template sibling" and kept out of
both the positives and the negatives.

**Negatives:** 2,000 random cross-case pairs of page assets, plus 500 random cross-case
pairs of ad screenshots (cases differ, bytes differ, seed 14).

EVAL_PLACEHOLDER

## Known limits

- Images are not yet split into regions: a creative embedded in a larger screenshot (an
  ad inside a feed capture) is compared only as a whole. Cropping changes pHash/dHash by up to
  22 bits and PDQ by up to 106 bits (table above). The embedding is more tolerant to crops.
- Mirroring is not handled (flip-invariant hashes were not added). The kits do not mirror
  images, and the embedding keeps cosine ≥ 0.79 for a horizontal flip.
- Phone screenshots of different ads from the same device and app share the status bar and
  feed layout. They are the closest negatives (cases 10 ↔ 14: pHash 18–24, PDQ 78–96,
  cosine up to 0.88). The linking engine should treat the screenshot frame as a weaker
  signal than the creative itself.
- PDQ on a resized screenshot whose height is not a multiple of 64 moves more than pHash or
  dHash (bank screenshot pair at different resolutions: PDQ 40 bits, pHash 6, dHash 4).
- SigLIP 2's download includes the text tower (1.5 GB), although only the vision tower is used.
  A vision-only export would be about half that size.
- Only images that ingestion lists as artefacts or that sit in HAR bodies are hashed. CSS
  background images and images inside MHTML are not extracted.
