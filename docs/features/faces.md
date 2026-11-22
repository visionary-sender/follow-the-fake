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

**Reference set.** `person_face` compares each face with a reference set of public figures that
scam ads impersonate. The set is built once, on a VM, from a names file that lives outside git
(`python -m ftf.features.faces.reference build --names <names.txt> --out <dir>`). For each name the
builder takes the Wikidata human (`Name=Qid` pins an ambiguous name), collects that person's
portraits (Wikidata P18 and the Commons category), keeps the JPEG and PNG files with exactly one
clear face (YuNet, score at least 0.9, at least 60 px) and keeps up to five whose face agrees with
the person's portrait. Only Wikidata and Wikimedia Commons are contacted. No organiser creative
is ever a reference, and no face is cropped from one.

The set is a directory: `manifest.json` (per image: name, source URL, file, licence, SHA-256),
`images/`, `embeddings.npy` (SFace, one row per image, precomputed) and `build-report.json` (per
name: Wikidata id, candidates, images kept, or the reason a name was not found). It is not in git
and not in the container image. It lives in `gs://follow-the-fake-data/outputs/visual/face-reference/`.
At run time:

- `FTF_FACE_REFERENCE=<dir>` points at the directory. Default: `<model_dir()>/face-reference`
  (`$FTF_MODEL_DIR/face-reference`; in the jury image `/opt/ftf-models/face-reference`). The old name
  `FTF_FACES_REFERENCE` is still read for one release, with a deprecation note on stderr.
- Layout the loader reads: `manifest.json` (`images`: name, source, file, licence, sha256; `file` is
  relative to the directory, e.g. `images/<hash>.jpg`), `images/`, `embeddings.npy` (row i for
  `images[i]`). The bucket folder `gs://follow-the-fake-data/outputs/visual/face-reference/` holds
  exactly this layout, so it copies as-is into the image.
- if `manifest.json` is missing, faces are still embedded (`face_embedding`), no `person_face` is
  emitted, and one note goes to stderr;
- if `embeddings.npy` is missing, the loader embeds the images itself (slower, same result).

The git tree keeps only `reference_manifest.example.json` (invented names on `.example` URLs) as
the format reference. Nothing is downloaded at scan time.

`ftf features prefetch` fills the YuNet and SFace weights into `model_dir()/opencv_zoo`; they are
checked against their pinned SHA-256 digests on download and by size on later runs. The reference
set is not part of prefetch (it moves to its own bucket, HACK-83).

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
| Reference images | Wikimedia Commons and Wikidata, per image in the manifest (source URL and licence recorded per image; not in git) | per image | in the bucket, not in git |

Weights download on first use into `ftf.features.common.model_dir()` and are checked against
pinned SHA-256 digests.

## Results

Measured on `ftf-visual` (Debian, e2-standard-8: 8 vCPUs, no GPU), with
`ftf features --dataset <indicator input> --families text,faces` on the challenge captures.

| Measure | Value |
|---|---|
| Captures in the input | 34 (768 artefacts, of which 420 decode as images) |
| Captures with a `person_face` | 26 (526 indicators, one per detected face) |
| Captures with a `brand` | 17 (118 indicators) |
| Captures with a `face_embedding` | 26 (526 faces, the same faces as `person_face`) |
| CPU per image, faces only | about 0.26 s: 107.6 s CPU (user 93.2 s, sys 14.4 s) over the 420 decodable images, including model and reference loading; 52.6 s wall |
| CPU per image, earlier faces-only run | 0.21 s on 394 organiser creatives (528 faces) |

Re-run on the organiser dataset (34 captures, 768 artefacts), with the reference set of 33 people and
96 images (`FTF_FACE_REFERENCE`, from the bucket). Before is the old organiser run; after is
`--families text,faces`, same dataset, same code, new reference:

| Measure | Before | After |
|---|---|---|
| Captures with a face | 27 | 26 |
| Faces (`face_embedding`) | 626 | 526 |
| Captures with a named `person_face` | 27 | 26 |
| Named `person_face` indicators | 626 | 526 |
| Best cosine, p5 / p50 / p95 | 0.138 / 0.252 / 0.546 | 0.192 / 0.296 / 0.717 |
| Best cosine ≥ 0.363 (SFace same-person cut-off) | 15.5 % | 26.8 % |

Best-cosine histogram (count of faces): before `<0.2` 137, `0.2–0.3` 317, `0.3–0.363` 75, `0.363–0.4` 26,
`0.4–0.5` 20; after `<0.2` 31, `0.2–0.3` 244, `0.3–0.363` 110, `0.363–0.4` 26, `0.4–0.5` 30.

Text family and faces, CPU: about 1 min wall on `ftf-visual` (2 min 30 s CPU) for the 34 captures.

## Known limits

- **Reference coverage.** The set covers 33 public figures (96 portraits): the figures named in the
  organiser cases plus people often impersonated in Polish investment ads. Two of the 35 names had
  no single-face portrait on Wikimedia Commons or Wikidata and are not in the set. A person outside
  the set still gets a `face_embedding`, which matches the same face across captures, and a
  `person_face` to the nearest reference person with a low cosine.
- **Reference images from the open web.** Portraits come from Wikidata and Wikimedia Commons with
  their source URL and licence recorded per image in the manifest (no licence filter applied: the
  owner's decision, HACK-83). The manifest is in git; the images are not, and live in the bucket.
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
