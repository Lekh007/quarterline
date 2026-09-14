# syntax=docker/dockerfile:1.7
#
# Quarterline container image (SPEC §29 local deployment).
#
# Two stages so the runtime layer carries no build toolchain and no uv:
# stage 1 resolves the locked dependency set into /app/.venv, stage 2 copies
# that venv onto a plain slim base.
#
# The image runs as a non-root user and defaults to the SQLite profile with a
# writable /app/storage volume. Point DATABASE_URL at the compose Postgres
# service to use the pgvector profile instead.

# ---------- stage 1: dependencies ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is cached until pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra postgres

# Then the project itself.
COPY src ./src
COPY README.md LICENSE alembic.ini ./
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres

# ---------- stage 2: runtime ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    STORAGE_DIR=/app/storage \
    DATABASE_URL=sqlite:////app/storage/quarterline.db

# curl is the healthcheck's only runtime need.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 quarterline

WORKDIR /app

COPY --from=builder --chown=quarterline:quarterline /app/.venv /app/.venv
COPY --chown=quarterline:quarterline src ./src
COPY --chown=quarterline:quarterline migrations ./migrations
COPY --chown=quarterline:quarterline data ./data
COPY --chown=quarterline:quarterline prompts ./prompts
COPY --chown=quarterline:quarterline alembic.ini README.md LICENSE ./

RUN mkdir -p /app/storage && chown quarterline:quarterline /app/storage
VOLUME ["/app/storage"]

USER quarterline
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Bind 0.0.0.0 inside the container only — publish to 127.0.0.1 on the host
# (see docker-compose.yml). SPEC §28: no authn/authz, not multi-user safe.
ENTRYPOINT ["quarterline"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
