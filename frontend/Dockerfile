FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Project metadata first for cache-friendly dep installs. NOTE: the root
# pyproject is shared between agent + frontend — Phase 4 splits this into
# uv workspaces so the frontend image isn't dragging boto3/langchain.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY frontend /app/frontend

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "--frozen", \
     "chainlit", "run", "/app/frontend/app.py", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--headless"]
