# syntax=docker/dockerfile:1

# --- builder: resolve and install dependencies with uv, isolated from the runtime image ---
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

# WORKDIR matches the runtime stage's path exactly (both /app): uv-generated
# console-script shims (e.g. .venv/bin/alembic) hardcode an absolute shebang
# to this build-time interpreter path, so copying .venv to a *different*
# path in the runtime stage silently breaks every console script — `python
# -m alembic` would still work, but so does just keeping the paths aligned.
WORKDIR /app
COPY pyproject.toml uv.lock ./

# --no-install-project: install only third-party deps here so this layer is
# cached across app-code changes; the project itself is installed after
# COPY-ing the source, below.
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

# --- runtime: slim image, non-root, only what's needed to run the API ---
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system fade && useradd --system --gid fade --create-home fade

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app ./app
COPY --from=builder /app/alembic ./alembic
COPY --from=builder /app/alembic.ini ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UPLOAD_DIR=/app/data/uploads

RUN mkdir -p /app/data/uploads && chown -R fade:fade /app
USER fade

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
