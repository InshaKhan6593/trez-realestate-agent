# The bot backend: one image, two Railway services (start commands set on each service, DEPLOY.md).
#   webhook: uvicorn app.webhook:app
#   worker:  arq app.worker.WorkerSettings
# The scraper (Node) is not in here: it runs on GitHub Actions.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent ./agent
COPY app ./app

# Railway sets PORT. The worker service overrides this command.
CMD ["sh", "-c", "uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8000}"]
