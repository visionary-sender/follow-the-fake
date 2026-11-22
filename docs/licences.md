# Licences of what ships in the container

The jury runs the image built from [`Dockerfile`](../Dockerfile) on `main`. Only what is in that
image counts (owner, 2026-11-21). Tools used only to build or collect data are not covered.
This page lists what the image carries, with licences and usage limits. The Python package table
is generated: [`LICENSES-THIRD-PARTY.md`](../LICENSES-THIRD-PARTY.md) (`scripts/vm.sh licences`,
checked by the licence test). Licences were checked on 2026-11-22.

## 1. Python packages

Generated: [`LICENSES-THIRD-PARTY.md`](../LICENSES-THIRD-PARTY.md). Permissive licences only, plus
the named exceptions in section 3.

## 2. System packages in the image

Base: `python:3.11-slim` pinned by digest (`sha256:07d4824036af…`), Debian 13 (trixie); 87
packages. Each keeps its Debian licence, which the package's `copyright` file states. The slim
image has these files removed, so read them from the Debian archive.

Added by the Dockerfile's `playwright install --with-deps --only-shell chromium`: 112 apt packages
(plus the Chromium headless shell `chromium_headless_shell-1248` and `ffmpeg-1013` under
`/opt/ms-playwright`). The list is from `dpkg-query` inside the image built on the workbench
(2026-11-22). Licences are the Debian `License:` fields, or, where a copyright file is free-form,
the licence names found in it. That is a keyword check, not a legal review.

### 2a. Chromium's own system libraries (Playwright's list)

| Package | Licence (Debian copyright) |
|---|---|
| libasound2t64 | LGPL-2.1+ |
| libatk-bridge2.0-0t64, libatk1.0-0t64, libatspi2.0-0t64 | AFL-2.1 or GPL-2+; LGPL-2+ and LGPL-2.1+ parts |
| libcairo2 | free-form: LGPL and MPL-1.1 |
| libcups2t64 | Apache-2.0 (GPL-2/LGPL-2 exception); BSD-2-Clause; Zlib |
| libdbus-1-3 | AFL-2.1 or GPL-2+; BSD-3-Clause; LGPL-2.1+; Expat |
| libdrm2 | free-form: MIT |
| libgbm1 | Mesa: MIT, BSD, SGI, Khronos; GPL-2 parts |
| libglib2.0-0t64 | LGPL-2.1+ and LGPL-2+; GPL-2+ parts; CC-BY-SA-3.0 parts |
| libnspr4 | MPL-2.0 |
| libnss3 | MPL-2.0; BSD-3-Clause; Zlib |
| libpango-1.0-0 | LGPL-2+ and LGPL-2.1+; Apache-2.0; OFL-1.1 fonts |
| libx11-6, libxcb1, libxcomposite1, libxdamage1, libxext6, libxfixes3, libxkbcommon0, libxrandr2 | free-form: X11 / MIT |

**Owner decision needed.** Several of these are LGPL or MPL (libasound2t64, libatk*, libcairo2,
libdbus-1-3, libglib2.0-0t64, libnspr4, libnss3, libpango-1.0-0). The owner's rule names only
certifi, Playwright, Chromium and its Node driver, so these system libraries are not covered yet.
Either name them as unmodified dynamically linked Debian libraries of Chromium, or change the
dependency set.

### 2b. Other packages `--with-deps` adds (tools group: Xvfb, fonts, Mesa, X11)

- Permissive (MIT, X11, BSD, Apache, ISC, Zlib, Expat, OFL, libpng, Unicode): `libbrotli1`,
  `libdrm-amdgpu1`, `libdrm-common`, `libdrm-intel1`, `libedit2`, `libexpat1`,
  `libfontenc1`, `libice6`, `libllvm19` (Apache-2.0 with LLVM exception), `libpixman-1-0`,
  `libpng16-16t64`, `libvulkan1`, `libwayland-server0`, `libx11-data`, `libx11-xcb1`, `libxau6`,
  `libxaw7`, `libxcb-dri3-0`, `libxcb-glx0`, `libxcb-present0`, `libxcb-randr0`,
  `libxcb-render0`, `libxcb-shm0`, `libxcb-sync1`, `libxcb-xfixes0`, `libxdmcp6`, `libxfont2`,
  `libsm6`, `libxi6`, `libxkbfile1`, `libxml2` (ISC, MIT), `libxmu6`, `libxpm4`, `libxrender1`,
  `libxshmfence1`, `libxt6t64`, `libxxf86vm1`, `xfonts-utils`, `xkb-data`, `x11-xkb-utils`,
  `xserver-common`, `xvfb`, `fonts-noto-color-emoji` (Apache-2.0; SIL-1.1).
- Contain copyleft parts (LGPL, GPL, MPL, AFL, GFDL, LPPL, or GPL with a font exception). The
  same owner decision as 2a applies: `at-spi2-common`, `fonts-freefont-ttf`, `fonts-ipafont-gothic`, `fonts-liberation` (GPL-2+ and OFL-1.1),
  `fonts-tlwg-loma-otf`, `fonts-unifont`, `fonts-wqy-zenhei`, `libasound2-data`,
  `libavahi-client3`, `libavahi-common-data`, `libavahi-common3`,
  `libcom-err2`, `libdatrie1`, `libelf1t64`, `libfreetype6`, `libfribidi0`, `libgl1`,
  `libgl1-mesa-dri`, `libglvnd0`, `libglx-mesa0`, `libglx0`, `libgnutls30t64`,
  `libgraphite2-3`, `libgssapi-krb5-2`, `libharfbuzz0b`, `libidn2-0`, `libk5crypto3`,
  `libkeyutils1`, `libkrb5-3`, `libkrb5support0`, `libp11-kit0`, `libpciaccess0`, `libsensors-config`,
  `libsensors5`, `libtasn1-6`, `libthai-data`, `libthai0`, `libunistring5`, `libunwind8`,
  `libz3-4`, `mesa-libgallium`, `x11-common`.
- Licence not determined: `libatomic1` (GCC runtime library), `fontconfig`, `fontconfig-config`,
  `libfontconfig1`, `xfonts-encodings` and `xfonts-scalable` (their copyright files have no
  licence names the keyword check finds).
  Check these from the Debian archive before the table is final.

### 2c. Not in the image on `main`

The feature-model slot in the Dockerfile (`FTF_WITH_MODELS`, off by default) would add `ffmpeg`,
`tesseract-ocr`, `tesseract-ocr-pol`, `tesseract-ocr-eng` and the weights in section 4. Their
licences must be added here before that slot is used.

## 3. Named exceptions

Owner rule (2026-11-21), the only exceptions to the permissive-only policy:

| Component | Licence | Terms we keep |
|---|---|---|
| certifi (CA bundle, pulled in by requests and tldextract) | MPL-2.0 | Used unmodified. MPL-2.0 is file-level copyleft and places no obligation on our code. Keep its notices. Recorded in `scripts/licences.py` (`EXCEPTIONS`). |
| Playwright (Python package and driver) | Apache-2.0 | Keep the licence and notices. Optional `fetch` extra only. |
| Chromium headless shell | BSD-3-Clause | Keep the notices. Bundles unmodified FFmpeg (LGPL-2.1): ship it unmodified, keep the LGPL text, and point to the upstream FFmpeg source. |
| Node.js runtime (inside Playwright) | MIT | Keep the notices. |

## 4. Models

Behind `FTF_WITH_MODELS` (off by default). The image on `main` downloads none of them: the build
fails if the flag is set. Each row links to its model card or licence file.

| Model | Source | Licence | Usage limits |
|---|---|---|---|
| multilingual-e5-small (text) | https://huggingface.co/intfloat/multilingual-e5-small | MIT (card field `license: mit`) | None stated beyond the licence. |
| siglip2-base-patch16-224 (image) | https://huggingface.co/google/siglip2-base-patch16-224 | Apache-2.0 (card field `license: apache-2.0`) | None stated beyond the licence. |
| OpenCV SFace (face recognition, ONNX) | https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface (licence: https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE) | Apache-2.0 (the directory README says all its files are Apache 2.0) | None stated beyond the licence. |

## 5. Outside services used at runtime

Usage terms and limits come from [`docs/enrichment.md`](enrichment.md) and
[`docs/scanner.md`](scanner.md). None of these services needs an API key or an account, except
BigQuery, which needs a Google Cloud project to bill.

| Service | Used by | Key | Terms (as recorded in the docs) | Limits |
|---|---|---|---|---|
| RDAP: IANA bootstrap `data.iana.org/rdap/dns.json`, then each registry's RDAP server | `ftf enrich-domain`: registrar, creation date | No | The IANA terms page returned 404, so no terms text was confirmed. Each registry sets its own policy. | Bootstrap: one request per run, cached 30 days. Registries: 1 request/s per host; retry 429 and 5xx with backoff, honouring `Retry-After`; cached 30 days. |
| DNS over HTTPS: `dns.google/resolve` (Google Public DNS) | `ftf enrich-domain`: A, NS, MX | No | Bound to the Google APIs Terms of Service. The page gives no numeric limit. The resolver queries the domain's nameservers, not the workbench. | 1 request/s; A, NS, MX per domain; cached 1 day. |
| crt.sh (`crt.sh/?q=…&output=json`) | `ftf enrich-domain`: issuers, first `not_before`, sibling names | No | No terms of use stated. Returns 429 and 502 under load. | 1 request per 5 s; 150 s timeout; 3 retries; cached 7 days. |
| CERT Polska warning list (`hole.cert.pl/domains/v2/domains.json`, about 22 MB) | `ftf enrich-domain` and `ftf scan --source certpl` | No | Usage rules at cert.pl/lista-ostrzezen: anyone may use the list for any purpose, commercial use included; no attribution required; no warranty. | No download limit stated. One download per run; cached 1 day. |
| Meta Ad Library search page (logged out, headless Chromium) | `ftf scan --source meta` (demonstration only; needs the optional `fetch` extra) | No key, no login | Meta's Automated Data Collection Terms (`facebook.com/robots.txt`) forbid automated collection without written permission. The source is a demonstration, not a production setup. The production route is the official Meta Ad Library API, which needs a developer account and identity confirmation; not used. | Observed from a cloud IP (2026-11-21): HTTP 403 JavaScript challenge, then the first page (about 30 ads per keyword); scrolling gets `Rate limit exceeded` (code 1675004). Scanner: `--delay` 5 s, `--max-ads` 150 per source per run, `--max-per-keyword` 50. |
| Google Ads Transparency Center RPC (`ftf scan --source google_atc`) | Scanner: creatives Google shows in Poland | No key; plain HTTPS | The docs record no terms text for this endpoint. Check before any production use. | Observed from a cloud IP (2026-11-21): HTTP 429 after about 59 creatives; the scanner marks the query `blocked` and skips the rest of the run. Scanner: `--delay` 5 s, `--max-ads` 150. |
| Google BigQuery public dataset `bigquery-public-data.google_ads_transparency_center.removed_creative_stats` (`ftf scan --source google_bq`) | Scanner: creatives Google removed in Poland | Needs a Google Cloud project to bill (`--bq-project`) and a token (`gcloud auth print-access-token`); no API key | Google's public dataset, the sanctioned bulk route. No rate limit stated. | One run scans about 3.4 GB (about 0.02 USD on demand; inside the free monthly terabyte). `maximumBytesBilled` caps a run at 10 GB. |

Services that need no key or account: RDAP, DNS over HTTPS, crt.sh, the CERT Polska list, the Meta
Ad Library page and the Google Ads Transparency Center RPC.
