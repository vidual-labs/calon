# syntax=docker/dockerfile:1

# Build stage — creates a Python 3.13 virtualenv and installs only production deps.
# This keeps the final image small: no build tools, no dev tools, no test runner.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# First, only the metadata needed for dependency resolution (pyproject + lock).
# This is a cache-busting layer: it rebuilds only when deps change.
COPY pyproject.toml uv.lock ./
RUN uv venv /app/.venv

# Now copy the full source and install the package (non-editable, so the code
# is baked into site-packages and the runtime image is self-contained).
COPY . .
RUN uv pip install --python /app/.venv/bin/python --system --no-editable .

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app /app

# The operator config and database are mounted at runtime (docker-compose maps
# them in). Here we only ensure the directories exist.
RUN mkdir -p /app/data /app/config \
    && useradd --uid 1000 --create-home --shell /bin/sh calon \
    && chown -R calon:calon /app

USER calon

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2); sys.exit(0 if r.status==200 else 1)"

# The app is launched with the factory so the lifespan (db migration, operator
# config sync, login-store build) runs before the server accepts requests.
CMD ["uvicorn", "calon.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--log-level", "info"]
