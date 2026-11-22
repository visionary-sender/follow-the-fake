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
# Extras of the ftf wheel: `app` is the Streamlit demo, `fetch` the safe URL fetcher (HACK-17).
# Later `features` (CPU models), e.g. --build-arg FTF_EXTRAS=app,fetch,features.
ARG FTF_EXTRAS=app,fetch
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
RUN pip install "$(ls /wheels/ftf-*.whl)[${FTF_EXTRAS}]" && rm -rf /wheels \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

# --- Slot: CPU feature models (Visual, HACK-13/14/15, not on main yet) -----------------------
# When the feature families land: apt ffmpeg tesseract-ocr tesseract-ocr-pol tesseract-ocr-eng,
# `features` in FTF_EXTRAS, and the weights (multilingual-e5-small, siglip2-base-patch16-224,
# SFace) baked into $FTF_MODEL_DIR so the jury's machine downloads nothing. Off by default.
ARG FTF_WITH_MODELS=0
RUN if [ "$FTF_WITH_MODELS" != 0 ]; then \
        echo "FTF_WITH_MODELS: model download is not implemented until the feature families are on main" >&2; \
        exit 1; \
    fi

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
