# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Builder stage: install deps + the package into /wheels.
# ---------------------------------------------------------------------------
# Pinned by digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim
#   docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
FROM python:3.13-slim@sha256:a0779d7c12fc20be6ec6b4ddc901a4fd7657b8a6bc9def9d3fde89ed5efe0a3d AS builder

WORKDIR /build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install runtime deps from requirements.in (pinned versions). v0.1 ships
# without a hash-locked file; the public-repo CI will generate one before
# 1.0. Versions are exact-pinned in requirements.in so builds are still
# deterministic for a given image build.
COPY requirements.in ./requirements.in
RUN pip install --no-cache-dir --target /wheels -r requirements.in

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --target /wheels --no-deps .

# ---------------------------------------------------------------------------
# Runtime stage.
# ---------------------------------------------------------------------------
FROM python:3.13-slim@sha256:a0779d7c12fc20be6ec6b4ddc901a4fd7657b8a6bc9def9d3fde89ed5efe0a3d AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/site-packages \
    PATH=/app/site-packages/bin:$PATH

# Non-root user with pinned UID 1000.
RUN groupadd --system --gid 1000 mcp \
    && useradd --system --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin mcp

WORKDIR /app
COPY --from=builder /wheels /app/site-packages
RUN chown -R mcp:mcp /app

USER mcp

EXPOSE 3716

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD ["python", "-m", "mcp_youtube.healthcheck"]

ENTRYPOINT ["python", "-m", "mcp_youtube.server"]
