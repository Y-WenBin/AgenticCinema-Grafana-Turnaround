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

# The base is pinned by digest, not by the `3.12-slim` tag, which is rebuilt
# weekly: without this the image is not reproducible and a base change lands in
# a deploy nobody made. The digest below is the multi-arch index for
# `python:3.12-slim` and resolves on both amd64 and arm64. To move it:
#
#   docker buildx imagetools inspect python:3.12-slim | head -2
#
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base

# --- mcp-grafana (pinned + checksum-verified, per architecture) ---------------
#
# Fetched in a throwaway stage so `curl` never reaches the runtime image.
#
# This used to hardcode `mcp-grafana_Linux_x86_64.tar.gz`. A `docker build` on
# Apple Silicon therefore produced an arm64 image carrying an amd64 binary: the
# agent tier came up and died on the first tool call with "exec format error",
# and the one step that would have caught it ended `|| true`, so it could not
# fail. Both halves are fixed here -- the tarball follows TARGETARCH, and the
# verification is allowed to fail the build.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS mcp
ARG TARGETARCH
ARG MCP_GRAFANA_VERSION=1.3.0
# From the release's own mcp-grafana_<version>_checksums.txt.
ARG MCP_GRAFANA_SHA256_AMD64=b9fc66e0613a4def86253627cb71b094ca35cf213ab18fa09a8d8bd578c197b1
ARG MCP_GRAFANA_SHA256_ARM64=c70c3d15514f1351cb5fb3e81b7e80e19fab229dda30b7ac1bce3cc812f838ec
#
# The verification below reads oddly on purpose. `mcp-grafana --help` may exit
# non-zero and that is its business; what must fail the build is the shell
# saying it could not *run* the file at all -- 126 ("cannot execute", which is
# what an amd64 ELF on arm64 gives) or 127 ("not found"). The `if` is load-
# bearing twice over: under `set -e` a bare failing command would end the shell
# before `status=$?` could run, and a comment cannot go inside the RUN because
# the continued lines are joined before the shell sees them.
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    case "${TARGETARCH}" in \
      amd64) slug=x86_64; sha="${MCP_GRAFANA_SHA256_AMD64}" ;; \
      arm64) slug=arm64;  sha="${MCP_GRAFANA_SHA256_ARM64}" ;; \
      *) echo "no mcp-grafana release for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/mcp-grafana.tar.gz \
      "https://github.com/grafana/mcp-grafana/releases/download/v${MCP_GRAFANA_VERSION}/mcp-grafana_Linux_${slug}.tar.gz"; \
    echo "${sha}  /tmp/mcp-grafana.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/mcp-grafana.tar.gz -C /usr/local/bin mcp-grafana; \
    chmod +x /usr/local/bin/mcp-grafana; \
    rm /tmp/mcp-grafana.tar.gz; \
    if /usr/local/bin/mcp-grafana --help >/dev/null 2>&1; then status=0; else status=$?; fi; \
    if [ "$status" -ge 126 ]; then \
      echo "mcp-grafana will not execute on ${TARGETARCH} (exit $status)" >&2; \
      exit 1; \
    fi

FROM base
COPY --from=mcp /usr/local/bin/mcp-grafana /usr/local/bin/mcp-grafana

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

# Non-root. Cloud Run's gVisor sandbox already bounds the blast radius, so this
# is defence in depth rather than the only wall -- but the process reads a
# service-account token out of the environment and spawns `mcp-grafana` as a
# subprocess, and neither needs uid 0 to do it.
#
# /app is left owned by root and merely readable: nothing at runtime writes into
# the image, and a read-only application directory means a compromised process
# cannot rewrite its own code. The write-back audit log used to sit inside the
# package and would have needed an exception here; it now defaults to the user's
# XDG state directory and, on a failure to write, returns a receipt rather than
# raising (`agent/writeback.py`). It is a CLI-only path (`--approve`) in any
# case, never reached by `agent/serve.py`, which runs AutoApprover(approve=False).
RUN useradd --system --create-home --uid 10001 turnaround
USER turnaround

# Cloud Run's SIGTERM: uvicorn handles graceful shutdown itself.
CMD ["python", "-m", "agent.serve"]
