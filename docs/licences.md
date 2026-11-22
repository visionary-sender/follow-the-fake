# Licences of what ships in the container

The image built from [`Dockerfile`](../Dockerfile) on `main`. Only what is in that
image counts. Tools used only to build or collect data are not covered.
This page lists what the image carries, with licences and usage limits. The Python package table
is generated: [`LICENSES-THIRD-PARTY.md`](../LICENSES-THIRD-PARTY.md) (`scripts/vm.sh licences`,
checked by the licence test). Licences were last checked against the current image.

## 1. Python packages

Generated: [`LICENSES-THIRD-PARTY.md`](../LICENSES-THIRD-PARTY.md). Permissive licences only, plus
the named exceptions in section 3. The check is `scripts/licences.py --check`, run by the licence
test. The Debian packages in section 2 are held to the same rule, with one named exception (2a).

## 2. System packages in the image

Base: `python:3.11-slim` pinned by digest (`sha256:07d4824036af…`), Debian 13 (trixie); 87
packages. Each keeps its Debian licence, which its `copyright` file states. The slim image has
these files removed, so read them from the Debian archive. **Not covered by a named exception:**
the base packages include GPL and LGPL ones (for example `bash` and `coreutils`, GPL-3+, and
`libc6`, LGPL-2.1+). They are not covered by a named exception and are listed here for review.

Added by the Dockerfile's `playwright install --with-deps --only-shell chromium`: 112 Debian
packages, plus the Chromium headless shell `chromium_headless_shell-1248` and `ffmpeg-1013` under
`/opt/ms-playwright`. The list is the difference between the base image and the built `ftf:dev`
image (`dpkg-query`), built on the cloud VM.

### 2a. Debian system libraries of Chromium (named exception)

Named exception: the Debian system libraries that Chromium needs, installed
by `playwright install --with-deps`, are allowed in the image even where they are LGPL, MPL or GPL
in part. They are unmodified Debian packages, and no package is patched. The list is every package
that the `--with-deps` step adds, as the issue defines it: some are not linked by Chromium itself
(X11 utilities, fonts, Xvfb). Narrowing the list would be a separate change. This is the only exception
for system packages; it does not extend to other packages. In
`scripts/licences.py` a package is accepted only if `scripts/chromium-debian-packages.tsv` lists
it with exactly the licence recorded there. `scripts/licences.py --audit-image FILE` checks the
packages of a built image against that list; the last audited image gives 0 errors. It is
run by hand on the VM, not by the licence test.
Licence names come from each copyright file's `License:` fields, or from its notice phrases
where the file is free-form. That is a keyword check, not a legal review.

Regenerate the list after a rebuild (on the VM, after `docker build -t ftf:dev .`):
`scripts/vm.sh run -- sh scripts/chromium_packages.sh ftf:dev > rows.tsv`, then review and commit
the changes as `scripts/chromium-debian-packages.tsv`.

| Package | Licence (copyright file) |
|---|---|
| at-spi2-common | AFL-2.1; AFL-2.1 or GPL-2+; GPL-2; GPL-2+; LGPL-2+; LGPL-2.1+; public-domain |
| fontconfig | free-form: HPND |
| fontconfig-config | free-form: HPND |
| fonts-freefont-ttf | GPL-3+; GPL-3+ with Special Font Exception |
| fonts-ipafont-gothic | GPL-3+; IPA-1 |
| fonts-liberation | GPL-2+; SIL-OFL-1.1 |
| fonts-noto-color-emoji | Apache-2.0; SIL-1.1 |
| fonts-tlwg-loma-otf | GPL-2+; GPL-2+ with Font exception; LPPL; other |
| fonts-unifont | GFDL-NIV; GPL-2+ |
| fonts-wqy-zenhei | GPL-2 with Font embedding exception; GPL-2 with Font embedding exception and M+ FONTS License; M+ FONTS License |
| libasound2-data | LGPL-2.1+ |
| libasound2t64 | LGPL-2.1+ |
| libatk-bridge2.0-0t64 | AFL-2.1; AFL-2.1 or GPL-2+; GPL-2; GPL-2+; LGPL-2+; LGPL-2.1+; public-domain |
| libatk1.0-0t64 | AFL-2.1; AFL-2.1 or GPL-2+; GPL-2; GPL-2+; LGPL-2+; LGPL-2.1+; public-domain |
| libatomic1 | free-form: MIT, GPL, LGPL, BSD |
| libatspi2.0-0t64 | AFL-2.1; AFL-2.1 or GPL-2+; GPL-2; GPL-2+; LGPL-2+; LGPL-2.1+; public-domain |
| libavahi-client3 | free-form: GPL, LGPL, BSD |
| libavahi-common-data | free-form: GPL, LGPL, BSD |
| libavahi-common3 | free-form: GPL, LGPL, BSD |
| libbrotli1 | MIT |
| libcairo2 | free-form: HPND, LGPL, MPL |
| libcom-err2 | 0BSD; Apache-2; BSD-3-Clause; BSD-3-Clause-Variant; BSD-4-Clause-CMU; Expat; GPL or MIT-US-export; GPL-2; GPL-2+; GPL-2+ with Texinfo exception; ISC; Kazlib; LGPL-2; Latex2e |
| libcups2t64 | Apache-2.0; Apache-2.0-with-GPL2-LGPL2-Exception; Apache-2.0-with-GPL2-LGPL2-Exception or BSD-2-clause; BSD-2-Clause; FSFUL; Zlib |
| libdatrie1 | GPL-2+; LGPL-2.1+ |
| libdbus-1-3 | AFL-2.1; BSD-3-clause; BSD-3-clause and BSD-3-clause-generic; BSD-3-clause-generic; Expat; GPL-2+; GPL-2+ or AFL-2.1, and Expat and Tcl-BSDish; LGPL-2.1+; Tcl-BSDish; g10-permissive |
| libdrm-amdgpu1 | free-form: MIT |
| libdrm-common | free-form: MIT |
| libdrm-intel1 | free-form: MIT |
| libdrm2 | free-form: MIT |
| libedit2 | BSD-3-clause |
| libelf1t64 | BSD-2-clause; GFDL-NIV-1.3; GPL-3+; GPL-3+ with Bison exception; LGPL-2.1+; LGPL-3+ or GPL-2+ |
| libexpat1 | MIT |
| libfontconfig1 | free-form: HPND |
| libfontenc1 | free-form: MIT |
| libfreetype6 | BSD-3-Clause; BSL-1.0; Expat; FSFAP; FTL; FTL and Expat; GPL-2+; GPL-3+; MIT-Modern-Variant; MIT-SMC; OpenGroup-MIT; Public-Domain; Zlib |
| libfribidi0 | LGPL-2.1+ |
| libgbm1 | Apache-2.0; BSD-2-clause; BSD-3-google; BSL; GPL; GPL-1+; GPL-2; GPL-2 or MIT; Khronos; MIT; MLAA; SGI |
| libgl1 | Apache-2.0; BSD-1-clause; GPL; GPL-3+; MIT; public-domain |
| libgl1-mesa-dri | Apache-2.0; BSD-2-clause; BSD-3-google; BSL; GPL; GPL-1+; GPL-2; GPL-2 or MIT; Khronos; MIT; MLAA; SGI |
| libglib2.0-0t64 | AFL-2.0; AFL-2.0 or LGPL-2.1+; Apache-2.0 with LLVM exception; CC-BY-SA-3.0; CC0-1.0; CC0-1.0 or Mingw-PD; Expat; FSFULLR; GPL-2+; Iconv-PD; Janik-permissive; Janik-permissive and old-glib-tests; Kuchling-PD; LGPL-2+; LGPL-2.1+; LGPL-2.1+ and Kuchling-PD and Plumb-PD; LGPL-3+; Mingw-PD; Plumb-PD; Unicode-DFS-2016; bzip2-1.0.6; cmph; old-glib-tests |
| libglvnd0 | Apache-2.0; BSD-1-clause; GPL; GPL-3+; MIT; public-domain |
| libglx-mesa0 | Apache-2.0; BSD-2-clause; BSD-3-google; BSL; GPL; GPL-1+; GPL-2; GPL-2 or MIT; Khronos; MIT; MLAA; SGI |
| libglx0 | Apache-2.0; BSD-1-clause; GPL; GPL-3+; MIT; public-domain |
| libgnutls30t64 | Apache-2.0; BSD-3-Clause; CC0 license; Expat; FSFAP; GPLv3+; LGPLv2.1+; LGPLv3+_or_GPLv2+; The main library is licensed under GNU Lesser |
| libgraphite2-3 | Artistic; Artistic or GPL-1+; GPL-1+; GPL-2+; LGPL-2.1+; LGPL-2.1+ or GPL-2+ or MPL-1.1; LGPL-2.1+ or MPL-1.1 or GPL-2+; MPL-1.1; custom-sil-open-font-license; public-domain |
| libgssapi-krb5-2 | free-form: MIT, GPL, BSD |
| libharfbuzz0b | Apache-2.0; CC0-1.0; Expat; FSFAP; FSFUL; FSFULLR; GPL-2+ with AutoConf exception; GPL-2+ with Font exception; GPL-2+ with LibTool exception; GPL-3+; GPL-3+ with AutoConf exception; ISC; LGPL-2.1+; MIT; Monotype; OFL-1.1; UFL-1.0; Unicode |
| libice6 | free-form: X11, HPND |
| libidn2-0 | Expat; GPL-2+; GPL-3+; LGPL-3+; LGPL-3+ or GPL-2+; Unicode |
| libk5crypto3 | free-form: MIT, GPL, BSD |
| libkeyutils1 | GPL-2+; LGPL-2+ |
| libkrb5-3 | free-form: MIT, GPL, BSD |
| libkrb5support0 | free-form: MIT, GPL, BSD |
| libllvm19 | APACHE-2-LLVM-EXCEPTIONS; BSD-3-Clause; BSD-3-clause; MIT; Python; solar-public-domain |
| libnspr4 | MPL-2.0 |
| libnss3 | BSD-3; MPL-2.0; Zlib; public-domain |
| libp11-kit0 | Apache-2.0; BSD-3-clause; FSFAP; FSFULLR; GPL-2+ with Autoconf-data exception; GPL-3+ with Autoconf-data exception; ISC; LGPL-2.1+; X11; customFSFUL; customFSFULLRWD |
| libpango-1.0-0 | Apache-2; Apache-2 and Bitstream-Vera and OFL-1.1; Bitstream-Vera; Chromium-BSD-style; Example; ICU; LGPL-2+; LGPL-2+ and ICU; LGPL-2+ and LGPL-2.1+; LGPL-2+ and TCL; LGPL-2.1+; OFL-1.1; TCL; Unicode |
| libpciaccess0 | free-form: MIT, GPL |
| libpixman-1-0 | free-form: MIT |
| libpng16-16t64 | Apache-2.0; BSD-3-clause; BSD-like-with-advertising-clause; GPL-2+; GPL-2+ or BSD-like-with-advertising-clause; expat; libpng; libpng OR Apache-2.0 OR BSD-3-clause |
| libsensors-config | GPL-2; GPL-2+; LGPL-2.1+; Linux-man-pages-copyleft |
| libsensors5 | GPL-2; GPL-2+; LGPL-2.1+; Linux-man-pages-copyleft |
| libsm6 | free-form: HPND |
| libtasn1-6 | free-form: GPL, LGPL |
| libthai-data | GPL-2+; LGPL-2.1+ |
| libthai0 | GPL-2+; LGPL-2.1+ |
| libunistring5 | BSD-3-clause or LGPL-2.1+; BSD-3-clause or LGPL-3+; FreeSoftware; GFDL-1.2+ or GPL-3+ or LGPL-3; GFDL-1.3+; GPL-2+; GPL-2+ with distribution exception; GPL-2+ with distribution exception, Expat and GPL-2+; GPL-3+; ISC or LGPL-2.1+; LGPL-2+; LGPL-2+ or LGPL-3+; LGPL-2.1+; LGPL-3; LGPL-3+; Unicode-DFS-2016; X11; bsd-3-clause; gfdl-1.2+; gfdl-1.3+; isc; public-domain; unicode-dfs-2016 |
| libunwind8 | Expat; GPL-2+ |
| libvulkan1 | Apache-2.0; MIT; MIT or Apache-2.0 |
| libwayland-server0 | X11 |
| libx11-6 | free-form: X11, MIT, HPND, BSD |
| libx11-data | free-form: X11, MIT, HPND, BSD |
| libx11-xcb1 | free-form: X11, MIT, HPND, BSD |
| libxau6 | free-form: HPND |
| libxaw7 | free-form: X11, MIT, HPND |
| libxcb-dri3-0 | free-form: MIT |
| libxcb-glx0 | free-form: MIT |
| libxcb-present0 | free-form: MIT |
| libxcb-randr0 | free-form: MIT |
| libxcb-render0 | free-form: MIT |
| libxcb-shm0 | free-form: MIT |
| libxcb-sync1 | free-form: MIT |
| libxcb-xfixes0 | free-form: MIT |
| libxcb1 | free-form: MIT |
| libxcomposite1 | free-form: MIT, HPND |
| libxdamage1 | free-form: HPND |
| libxdmcp6 | free-form: X11, HPND |
| libxext6 | free-form: X11, MIT, HPND |
| libxfixes3 | free-form: MIT, HPND |
| libxfont2 | free-form: MIT, HPND, BSD |
| libxi6 | free-form: MIT, HPND |
| libxkbcommon0 | free-form: MIT |
| libxkbfile1 | free-form: HPND |
| libxml2 | ISC; MIT-1 |
| libxmu6 | free-form: MIT, HPND |
| libxpm4 | free-form: MIT |
| libxrandr2 | free-form: HPND |
| libxrender1 | free-form: HPND |
| libxshmfence1 | free-form: HPND |
| libxt6t64 | free-form: X11, MIT, HPND |
| libxxf86vm1 | free-form: MIT |
| libz3-4 | Expat; GPL-2+ |
| mesa-libgallium | Apache-2.0; BSD-2-clause; BSD-3-google; BSL; GPL; GPL-1+; GPL-2; GPL-2 or MIT; Khronos; MIT; MLAA; SGI |
| x11-common | free-form: MIT, GPL |
| x11-xkb-utils | free-form: MIT, HPND |
| xfonts-encodings | free-form: public domain |
| xfonts-scalable | free-form: Bitstream font licence |
| xfonts-utils | free-form: MIT, HPND, BSD |
| xkb-data | free-form: X11, MIT, HPND |
| xserver-common | free-form: X11, MIT, HPND, BSD |
| xvfb | free-form: X11, MIT, HPND, BSD |

### 2b. Not in the image on `main`

The feature-model slot in the Dockerfile (`FTF_WITH_MODELS`, off by default) would add `ffmpeg`,
`tesseract-ocr`, `tesseract-ocr-pol`, `tesseract-ocr-eng` and the weights in section 4. Their
licences must be added here before that slot is used.

## 3. Named exceptions

The only exceptions to the permissive-only policy:

| Component | Licence | Terms we keep |
|---|---|---|
| certifi (CA bundle, pulled in by requests and tldextract) | MPL-2.0 | Used unmodified. MPL-2.0 is file-level copyleft and places no obligation on our code. Keep its notices. Recorded in `scripts/licences.py` (`EXCEPTIONS`). |
| Playwright (Python package and driver) | Apache-2.0 | Keep the licence and notices. Optional `fetch` extra only. |
| Chromium headless shell | BSD-3-Clause | Keep the notices. Bundles unmodified FFmpeg (LGPL-2.1): ship it unmodified, keep the LGPL text, and point to the upstream FFmpeg source. |
| Node.js runtime (inside Playwright) | MIT | Keep the notices. |
| Debian ffmpeg (features slot, `FTF_WITH_FFMPEG`) | GPL-2+ and GPL-3+ parts, LGPL-2.1+ parts, and permissive parts (the exact licence string is in `scripts/licences.py`, `SYSTEM_EXCEPTIONS`) | Named exception. Used unmodified, run as a separate program. Only this exact licence string is accepted; GPL is not allowed in general. |
| Debian system libraries of Chromium, 112 packages (section 2a) | LGPL, MPL, GPL and others, per package | Unmodified Debian packages, installed by `playwright install --with-deps`. Keep each package's copyright notices (`/usr/share/doc/<package>/copyright`). Recorded in `scripts/chromium-debian-packages.tsv`. |

## 4. Models

Behind `FTF_WITH_MODELS` (off by default). The image on `main` downloads none of them: the build
fails if the flag is set. Each row links to its model card or licence file.

| Model | Source | Licence | Usage limits |
|---|---|---|---|
| multilingual-e5-small (text) | https://huggingface.co/intfloat/multilingual-e5-small | MIT (card field `license: mit`) | None stated beyond the licence. |
| siglip2-base-patch16-224 (image) | https://huggingface.co/google/siglip2-base-patch16-224 | Apache-2.0 (card field `license: apache-2.0`) | None stated beyond the licence. |
| OpenCV SFace (face recognition, ONNX) | https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface (licence: https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE) | Apache-2.0 (the directory README says all its files are Apache 2.0) | None stated beyond the licence. |
| Face reference images (public figures, `FTF_FACE_REFERENCE`) | Wikidata and Wikimedia Commons, one or more portraits per listed figure | CC0 or CC BY, per image (recorded in the manifest) | Reference images of public figures used for demonstration. Not in git. |

## 5. Outside services used at runtime

Usage terms and limits come from [`docs/enrichment.md`](enrichment.md) and
[`docs/scanner.md`](scanner.md). None of these services needs an API key or an account, except
BigQuery, which needs a Google Cloud project to bill.

| Service | Used by | Key | Terms (as recorded in the docs) | Limits |
|---|---|---|---|---|
| RDAP: IANA bootstrap `data.iana.org/rdap/dns.json`, then each registry's RDAP server | `ftf enrich-domain`: registrar, creation date | No | The IANA terms page returned 404, so no terms text was confirmed. Each registry sets its own policy. | Bootstrap: one request per run, cached 30 days. Registries: 1 request/s per host; retry 429 and 5xx with backoff, honouring `Retry-After`; cached 30 days. |
| DNS over HTTPS: `dns.google/resolve` (Google Public DNS) | `ftf enrich-domain`: A, NS, MX | No | Bound to the Google APIs Terms of Service. The page gives no numeric limit. The resolver queries the domain's nameservers, not the cloud VM. | 1 request/s; A, NS, MX per domain; cached 1 day. |
| crt.sh (`crt.sh/?q=…&output=json`) | `ftf enrich-domain`: issuers, first `not_before`, sibling names | No | No terms of use stated. Returns 429 and 502 under load. | 1 request per 5 s; 150 s timeout; 3 retries; cached 7 days. |
| CERT Polska warning list (`hole.cert.pl/domains/v2/domains.json`, about 22 MB) | `ftf enrich-domain` and `ftf scan --source certpl` | No | Usage rules at cert.pl/lista-ostrzezen: anyone may use the list for any purpose, commercial use included; no attribution required; no warranty. | No download limit stated. One download per run; cached 1 day. |
| Meta Ad Library search page (logged out, headless Chromium) | `ftf scan --source meta` (demonstration only; needs the optional `fetch` extra) | No key, no login | Meta's Automated Data Collection Terms (`facebook.com/robots.txt`) forbid automated collection without written permission. The source is a demonstration, not a production setup. The production route is the official Meta Ad Library API, which needs a developer account and identity confirmation; not used. | Observed from a cloud IP: HTTP 403 JavaScript challenge, then the first page (about 30 ads per keyword); scrolling gets `Rate limit exceeded` (code 1675004). Scanner: `--delay` 5 s, `--max-ads` 150 per source per run, `--max-per-keyword` 50. |
| Google Ads Transparency Center RPC (`ftf scan --source google_atc`) | Scanner: creatives Google shows in Poland | No key; plain HTTPS | The docs record no terms text for this endpoint. Check before any production use. | Observed from a cloud IP: HTTP 429 after about 59 creatives; the scanner marks the query `blocked` and skips the rest of the run. Scanner: `--delay` 5 s, `--max-ads` 150. |
| Google BigQuery public dataset `bigquery-public-data.google_ads_transparency_center.removed_creative_stats` (`ftf scan --source google_bq`) | Scanner: creatives Google removed in Poland | Needs a Google Cloud project to bill (`--bq-project`) and a token (`gcloud auth print-access-token`); no API key | Google's public dataset, the sanctioned bulk route. No rate limit stated. | One run scans about 3.4 GB (inside the free monthly terabyte). `maximumBytesBilled` caps a run at 10 GB. |

Services that need no key or account: RDAP, DNS over HTTPS, crt.sh, the CERT Polska list, the Meta
Ad Library page and the Google Ads Transparency Center RPC.
