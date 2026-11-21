# Feature family `faces`: people and brands

Module `ftf.features.faces` (HACK-15). It finds the faces in ad creatives and landing pages,
names the public figure each face most resembles from a small reference set, and names the
news outlet, TV channel, bank, company or institution the page invokes. Like every feature
family it exports raw similarity inputs only; the linking engine decides what counts as a match.

## Indicators

All are layer `creative`, level `campaign`, extractor `faces@0.1`.

| Kind | Value / vector | Provenance |
|---|---|---|
| `face_embedding` | 128-d SFace vector (model `opencv_zoo/face_recognition_sface_2021dec`), stored L2-normalised | locator `face=<i>;bbox=x,y,w,h` (original pixels), note `det=<detector score>` |
| `person_face` | name of the best-matching reference person, for **every** face, however low the similarity | same locator; note `sface_cosine=<x>;top3=<name>:<cos>\|…;det=<score>;ref=<manifest sha>` |
| `brand` | canonical outlet or brand name (e.g. `TVN24`, `Bank Pekao`) | locator `src=<sources>` (`ocr_text`, `page_text`, `html_title`, `ocr`), note `category=<news\|tv\|bank\|company\|public>;matches=<source>:<matched span>…` |

No threshold is applied. For calibration: the SFace authors' same-person cut-off is cosine
0.363 on LFW-style photos; screenshots of ads are smaller and recompressed, so the linking
engine should calibrate on its own pairs. `person_face` should be weighted low in linking:
the same celebrity is used by many unrelated campaigns (Meta uses face match only as a
confirmatory step, see knowledge base 05).

`features_for_image(path, text=None, ocr=True)` returns the same features for one still image
(used by the video family on keyframes; brands come from `text` or a tesseract pass).

## Method

**Faces.** Images are decoded with Pillow as inert pixels (first frame only, a pixel limit
against decompression bombs). YuNet detects faces (detector confidence ≥ 0.7, which defines
what a face is, not a similarity cut). Wide images are scaled to 1280 px; tall full-page
screenshots are scanned in overlapping square tiles so faces keep their size (at most 40 tiles),
and detections duplicated in the overlaps are merged. Each face is aligned on its five landmarks
and embedded with SFace.

**Reference set.** `src/ftf/features/faces/reference_manifest.json` lists the people
(name, Wikidata id) and for each the Wikimedia Commons images used, with source page,
author, licence and licence URL. Only public domain, CC0 and CC BY are accepted (no BY-SA,
NC or ND; `licence_ok`, enforced again at build time). The manifest was drafted by
`python -m ftf.features.faces.reference select`: it takes the person's Wikidata portrait (P18)
and the files of their Commons category, keeps the licence-clean JPEG/PNG files with exactly one
clear face, and keeps up to 5 whose face agrees with the Wikidata portrait (or the medoid). No
organiser image is ever used as a reference. Images and embeddings are not in git:

- built on first use from the manifest (downloads ~500 px Commons thumbnails, embeds the
  largest face) into `$FTF_MODEL_DIR/faces-reference/<manifest sha>/`;
- or read from a prebuilt copy given by `FTF_FACES_REFERENCE=<dir>`. Ours is at
  `gs://follow-the-fake-data/outputs/visual/hack-15/reference/` (`embeddings.npy`, `rows.json`).
- `FTF_OFFLINE=1` skips the build; if the build fails (no network, Wikimedia throttling) the
  family still emits `face_embedding` and `brand`, just no `person_face`.

Build it ahead of time with `python -m ftf.features.faces.reference build` (e.g. in a container
build step). Wikimedia throttles bulk thumbnail downloads; the builder paces itself and honours
`Retry-After`.

**Brands.** A gazetteer in `brands.py` of about 50 Polish news portals, TV and radio, banks,
large companies and public institutions that scam pages impersonate (general public knowledge;
names only). Long names match case-insensitively on word boundaries; short or ambiguous ones
(`WP`, `ING`, `PKO`, `FAKT`, `NBP`, `ZUS`, …) only in capitals or in their domain form, and a
more specific brand subsumes its parent (`TVN24` over `TVN`). Text sources, per capture:

1. `ocr_text` and `page_text` indicators from the `text` family (HACK-13), which runs first;
2. the saved page's `<title>` and `og:site_name`/`og:title`, read with a regex over the first
   512 KB of bytes (no HTML parser, nothing rendered or executed);
3. if a screenshot has no `ocr_text` indicator, a tesseract (`pol+eng`) pass of our own.

Logo matching (no text) is not implemented; see limits.

## Models and dependencies

| Item | Id / source | Licence | Download size |
|---|---|---|---|
| YuNet face detector | `opencv_zoo/face_detection_yunet_2023mar` (github.com/opencv/opencv_zoo) | MIT | 233 KB |
| SFace face recogniser | `opencv_zoo/face_recognition_sface_2021dec` (same) | Apache-2.0 | 38.7 MB |
| opencv-python-headless | PyPI, `features` extra | Apache-2.0 (bundled FFmpeg LGPL-2.1, dynamically linked) | ~50 MB wheel |
| Pillow | PyPI, `features` extra | MIT-CMU (HPND) | ~4.5 MB wheel |
| Tesseract + `pol`, `eng` data | Debian packages (optional, OCR fallback) | Apache-2.0 | ~20 MB |
| Reference images | Wikimedia Commons, per image in the manifest | PD / CC0 / CC BY (attribution in the manifest) | ~__REF_SIZE__ |

Weights download on first use into `ftf.features.common.model_dir()` and are checked against
pinned SHA-256 digests.

## Results

__RESULTS__

## Known limits

- **Reference coverage.** Only people with licence-clean, single-face Commons photos are in the
  set. __REF_LIMITS__ A face of anyone else still gets a `person_face` (its nearest reference,
  with a low cosine) and, more usefully, a `face_embedding` that matches the same face in other
  captures.
- **Small faces.** YuNet is trained for faces of about 10–300 px; avatars under ~20 px and
  heavily stylised or AI-generated faces are missed or embed poorly.
- **Brands without text.** A brand shown only as a logo image (no OCR-readable name, no title)
  is not found. Logo matching against licensed logo crops was left out for time.
- **Gazetteer, not a classifier.** A brand named in a fake comment or a "featured in" strip
  counts the same as the impersonated masthead; the `src` and `matches` fields let the linker
  tell the sources apart.
- **Container.** The project Dockerfile does not yet install the `features` extra, tesseract or
  the reference data (owned by the Demo project, HACK-32). Until it does, the container emits
  no `faces` indicators.
