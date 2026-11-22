# Follow the Fake: image that runs the `ftf` command and the demo app (CPU only, no keys).
#   docker build -t ftf .
#   docker run --rm ftf --help                       # any `ftf` command
#   docker run ... ftf                               # no arguments: build base if needed, serve app
# Run it with the hardened flags from the README ("Run it with Docker (jury)", "Safe URL fetcher").

# Base image pinned by digest (python:3.11-slim as pulled on 2026-11-21) for a reproducible build.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535 AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
# Extras of the ftf wheel: `app` is the Streamlit demo, `fetch` the safe URL fetcher (HACK-17),
# `features` the CPU feature models (HACK-13/14/15/16). --build-arg FTF_EXTRAS=app,fetch leaves
# `features` out (no torch, no weights).
ARG FTF_EXTRAS=app,fetch,features
# torch CPU build from the PyTorch index (no CUDA wheels); used only when FTF_EXTRAS has `features`.
ARG TORCH_VERSION=2.15.0
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    FTF_RAW_ROOT=/data/raw \
    FTF_BASE_DIR=/base \
    FTF_MODEL_DIR=/opt/ftf-models \
    FTF_BRAND_STOPLIST=/opt/ftf-brand/brand-stoplist.json \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none
RUN useradd --create-home --uid 10001 ftf \
    && mkdir -p /data/raw /base /opt/ftf-models \
    && chown ftf:ftf /base
COPY --from=build /wheels /wheels
# --- Safe URL fetcher (HACK-17) --------------------------------------------------------------
# `ftf fetch` needs Playwright and its headless Chromium (BSD) plus the system libraries it links.
RUN if [ "${FTF_EXTRAS#*features}" != "$FTF_EXTRAS" ]; then \
        pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}+cpu"; \
    fi \
    && pip install "$(ls /wheels/ftf-*.whl)[${FTF_EXTRAS}]" && rm -rf /wheels \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

# --- Slot: CPU feature models (Visual, HACK-13/14/15/16, HACK-82) ----------------------------
# The jury image bakes the weights (FTF_WITH_MODELS=1, default) so the jury's machine downloads
# nothing: tesseract (pol, eng) and ffmpeg from apt, then `ftf features prefetch` fills
# $FTF_MODEL_DIR (multilingual-e5-small, siglip2-base-patch16-224, YuNet, SFace, reference set).
# FTF_WITH_MODELS=0 skips both; the features then degrade to hashes only. FTF_WITH_FFMPEG=0
# leaves ffmpeg out (its Debian build is GPL; licence decision pending, HACK-74).
ARG FTF_WITH_MODELS=1
ARG FTF_WITH_FFMPEG=1
RUN if [ "$FTF_WITH_MODELS" != 0 ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-pol tesseract-ocr-eng \
           $([ "$FTF_WITH_FFMPEG" != 0 ] && echo ffmpeg) \
        && rm -rf /var/lib/apt/lists/* \
        && ftf features prefetch --model-dir /opt/ftf-models \
        && chmod -R a+rX /opt/ftf-models; \
    fi
# Face reference set (HACK-83): filled on the workbench by scripts/fetch_face_reference.sh into the
# gitignored build context. The folder may be empty (the build still works; faces are then embedded
# but not named).
COPY docker/build-context/faces-reference/ /opt/ftf-models/face-reference/
RUN chmod -R a+rX /opt/ftf-models
# Offline at run time: the weights are read from $FTF_MODEL_DIR and the reference set from
# $FTF_FACE_REFERENCE; nothing is fetched from the hub. Global FTF_OFFLINE is not set (it would also
# switch off the jury's live enrichment); the model loaders honour it on their own paths.
# Visual's HACK-83 makes FTF_FACE_REFERENCE the name; the plural stays as an alias.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    FTF_FACE_REFERENCE=/opt/ftf-models/face-reference \
    FTF_FACES_REFERENCE=/opt/ftf-models/face-reference

COPY docker/entrypoint.sh /usr/local/bin/ftf-entrypoint
RUN chmod 0755 /usr/local/bin/ftf-entrypoint

# --- Brand stop-list (HACK-75) ---------------------------------------------------------------
# Hashed values of real outlet pages, read by the linking engine from FTF_BRAND_STOPLIST. It is
# fetched into docker/brand-stoplist/ on the build host (scripts/fetch-brand-stoplist.sh) and is
# never in git. The folder always exists (.gitkeep), so the build works without the file; then
# the engine warns at run time and scores without the stop-list (the platform/creative gate still
# holds the brand-negative result).
COPY docker/brand-stoplist/ /opt/ftf-brand/

USER ftf
WORKDIR /home/ftf
EXPOSE 8501
ENTRYPOINT ["ftf-entrypoint"]
CMD []
