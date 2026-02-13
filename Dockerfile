FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first for better caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy operator source
COPY operator.py .
RUN uv sync --frozen --no-dev

# Run as non-root
RUN groupadd --system appuser && useradd --system --gid appuser --create-home appuser
USER appuser

ENTRYPOINT ["uv", "run", "kopf", "run", "/app/operator.py", "--all-namespaces", "--verbose"]
