FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first for better caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy operator source
COPY lb_operator/ lb_operator/
RUN uv sync --frozen --no-dev

# Run as non-root
RUN groupadd --system appuser && useradd --system --gid appuser --create-home appuser
USER appuser

ENTRYPOINT ["uv", "run", "python3", "-m", "lb_operator"]
