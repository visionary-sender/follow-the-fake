# Follow the Fake: image that runs the `ftf` command.
#   docker build -t ftf .
#   docker run --rm ftf --help

FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11-slim
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 ftf
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

USER ftf
WORKDIR /home/ftf
ENTRYPOINT ["ftf"]
CMD ["--help"]
