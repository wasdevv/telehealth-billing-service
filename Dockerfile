# syntax=docker/dockerfile:1
# The same image runs the API, the Celery worker and beat; the command decides which.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y curl libpq5 && \
    rm -rf /var/lib/apt/lists/*

# --- dependencies ---------------------------------------------------------------------
FROM base AS build

# Lock file first, source second: editing a view must not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime --------------------------------------------------------------------------
FROM base

COPY --from=build /app /app

# Nothing here needs root, and a container that cannot rewrite its own code is a smaller
# blast radius if anything in it is ever exploited.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER 1000:1000

EXPOSE 8000

# Two workers is a starting point, not a tuning: raise it once you have measured.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
