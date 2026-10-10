FROM python:3.11-slim AS builder

# Pull the standalone uv/uvx binaries instead of installing via pip.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so they're cached independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM builder AS test

COPY tests ./tests
RUN uv sync --frozen --dev && uv run pytest -q && uv run pyright

FROM python:3.11-slim AS runtime

RUN addgroup --system app && adduser --system --ingroup app --home /app app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
ENTRYPOINT ["mcp-markdown-repositories"]
