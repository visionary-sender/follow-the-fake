# Follow the Fake: image that runs the `ftf` command.
#   docker build -t ftf .
#   docker run --rm ftf --help
# For `ftf fetch` use the hardened run flags from the README ("Safe URL fetcher").

# Base image pinned by digest (python:3.11-slim as pulled on 2026-11-21) for a reproducible build.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535 AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN useradd --create-home --uid 10001 ftf
COPY --from=build /wheels /wheels
# `ftf fetch` needs Playwright and its headless Chromium (BSD) plus the system libraries it links.
RUN pip install "$(ls /wheels/ftf-*.whl)[fetch]" && rm -rf /wheels \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

USER ftf
WORKDIR /home/ftf
ENTRYPOINT ["ftf"]
CMD ["--help"]
