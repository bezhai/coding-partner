FROM python:3.11-slim

# Install system dependencies: git, script (bsdutils), and curl for Claude CLI install
RUN apt-get update && \
    apt-get install -y --no-install-recommends git bsdutils curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Claude CLI (users may also volume-mount their own binary)
# Uncomment the line below if you want to bake Claude CLI into the image:
# RUN curl -fsSL https://claude.ai/install.sh | sh

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY coding_partner/ coding_partner/

# Create data directory
RUN mkdir -p data

ENTRYPOINT ["uv", "run", "python", "-m", "coding_partner.main"]
