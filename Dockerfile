FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better caching
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy operator source
COPY operator.py .

# Run as non-root
RUN useradd --create-home --shell /bin/bash operator
USER operator

ENTRYPOINT ["kopf", "run", "/app/operator.py", "--all-namespaces", "--verbose"]
