# ---------------------------------------------------------------- builder
FROM python:3.13-slim AS builder

# Keep in sync with UV_VERSION in .github/workflows (a test enforces this).
ARG UV_VERSION=0.11.32

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

RUN pip install "uv==${UV_VERSION}"

WORKDIR /src
# Dependencies first, exactly as pinned in uv.lock, so code-only changes reuse
# this layer. --locked fails the build if uv.lock is out of date with pyproject.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python3
COPY app ./app
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python3

# ---------------------------------------------------------------- runtime
FROM python:3.13-slim

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="WishPanel" \
      org.opencontainers.image.description="Request portal for comics, manga and manhwa" \
      org.opencontainers.image.source="https://github.com/Jeffreybalagot/wishpanel" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -o -g 100 wishpanel \
 && useradd -o -u 99 -g wishpanel -d /config -M -s /usr/sbin/nologin wishpanel

COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 0755 /entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PORT=8080 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=UTC

VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=3s --retries=3 \
  CMD ["python", "/usr/local/bin/healthcheck.py"]

# The entrypoint starts as root only to apply PUID/PGID and fix /config
# ownership, then drops privileges; the app itself never runs as root.
# Running with `--user 99:100` also works and skips the remap entirely.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
