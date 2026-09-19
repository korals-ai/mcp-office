# Toolspace sidecar (office): a per-chat co-located helper container that
# owns document conversion (POSTed to a shared Collabora Online service —
# no LibreOffice in this image), poppler-utils
# (PDF page rasterization + text) and the python office authoring libraries,
# exposed as MCP tools over Streamable HTTP (http://localhost:8090/mcp).
#
# Design + rationale: docs/plan/20260619-200506-toolspace-sidecar.md
# (the "orbital workspace-tools" sidecar substrate; §6b for the MCP-over-HTTP +
# shared-PVC split). There may be several workspace-tool-* sidecar images, one
# per tool family, all following this layout.

FROM python:3.12-slim AS py-builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc musl-dev \
  && rm -rf /var/lib/apt/lists/*

COPY workspace-tools/office/requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

# poppler-utils (pdftoppm/pdftotext) plus fonts for rasterizing PDFs whose
# text is not embedded. Conversion needs nothing here — it is a POST to the
# shared office service.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      tini \
      fonts-dejavu \
      fonts-liberation \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=py-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=py-builder /usr/local/bin /usr/local/bin

COPY workspace-tools/office/src ./src
# The one log format every tool image installs (apps/workspace-tools/toollog).
# COPY'd next to src, imported as `toollog` under `python -m` from /app — the
# same shape connector_base uses. Stdlib-only, so it adds no requirements.
COPY workspace-tools/toollog ./toollog

# The shared stalled-loop watchdog (apps/workspace-tools/loopwatch). Same
# placement and import rules as toollog above: COPY'd next to src, imported
# as `loopwatch` under `python -m` from /app.
COPY workspace-tools/loopwatch ./loopwatch

# The bounded-tool runner (apps/workspace-tools/toolbound): runs a synchronous
# @mcp.tool() off the event loop under a hard timeout. Same placement and
# import rules as toollog above. `import toolbound` in src/server.py with no
# COPY here shipped seven images that died at import (2026-09-18 incident).
COPY workspace-tools/toolbound ./toolbound


ENV PYTHONPATH=/app

# Mirror the workspace pod's unprivileged identity (uid/gid 65532) so that
# when co-located in the workspace pod sharing the tenant PVC subPath, files
# the tools write carry the ownership the main container expects (fsGroup
# 65532). See workspace Dockerfile + apps/workspace-operator podSpec
# securityContext.
RUN groupadd --system --gid 65532 tool \
 && useradd --system --uid 65532 --gid 65532 --home-dir /home/tool --shell /bin/bash tool \
 && mkdir -p /home/tool \
 && chown -R tool:tool /home/tool
ENV HOME=/home/tool

EXPOSE 8090

USER tool

# PID 1 drops any signal it has no handler for, so our code never runs as
# PID 1: tini does, forwarding SIGTERM and reaping orphans.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "src.server"]
