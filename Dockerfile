# Turnaround agent — Cloud Run image.
#
# This file lives at the repo root on purpose. `gcloud run deploy --source .`
# builds a Dockerfile only if it finds one in the *source root*; anywhere else
# it silently falls back to buildpacks and produces an image with no
# `mcp-grafana` binary and the wrong entrypoint. tests/test_reproducibility.py
# pins the location.
#
# Runs agent/serve.py (FastAPI on $PORT). The deployed path is the OSS Grafana
# MCP mode, so the image bundles the `mcp-grafana` binary; ADK spawns it as a
# stdio subprocess (no ports, no sidecar). Vertex auth is the Cloud Run service
# account's ADC — no key file baked in.

FROM python:3.12-slim AS base

# --- mcp-grafana (pinned + checksum-verified) --------------------------------
ARG MCP_GRAFANA_VERSION=1.3.0
ARG MCP_GRAFANA_SHA256=b9fc66e0613a4def86253627cb71b094ca35cf213ab18fa09a8d8bd578c197b1
ADD https://github.com/grafana/mcp-grafana/releases/download/v${MCP_GRAFANA_VERSION}/mcp-grafana_Linux_x86_64.tar.gz /tmp/mcp-grafana.tar.gz
RUN echo "${MCP_GRAFANA_SHA256}  /tmp/mcp-grafana.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/mcp-grafana.tar.gz -C /usr/local/bin mcp-grafana \
 && chmod +x /usr/local/bin/mcp-grafana \
 && rm /tmp/mcp-grafana.tar.gz \
 && /usr/local/bin/mcp-grafana --help >/dev/null 2>&1 || true

# --- Python deps via uv -----------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Layer 1: dependency resolution only (cached unless the lock changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Layer 2: the project itself
COPY . .
RUN uv sync --frozen --no-dev

# --- runtime --------------------------------------------------------------
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TURNAROUND_MCP_MODE=oss \
    TURNAROUND_MCP_GRAFANA_BIN=/usr/local/bin/mcp-grafana \
    PORT=8080
EXPOSE 8080

# Cloud Run's SIGTERM: uvicorn handles graceful shutdown itself.
CMD ["python", "-m", "agent.serve"]
