# Toolspace sidecar (office): a per-chat co-located helper container that
# owns LibreOffice (document conversion) and poppler-utils (PDF page
# rasterization) and exposes both as MCP tools over Streamable HTTP
# (http://localhost:8090/mcp). The workspace agent calls it instead of
# running soffice/pdftoppm itself — letting the workspace image shed
# LibreOffice (~378 MB) in a later phase.
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

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

# LibreOffice + the native libs / fonts it needs to render Office docs
# headlessly. Keep on Debian (not Alpine) — LibreOffice packages not
# available/recent on Alpine.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libcairo2 \
      libgdk-pixbuf-2.0-0 \
      shared-mime-info \
      fonts-dejavu \
      fonts-liberation \
      fonts-crosextra-carlito \
      fonts-crosextra-caladea \
      libreoffice-writer \
      libreoffice-calc \
      libreoffice-impress \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=py-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=py-builder /usr/local/bin /usr/local/bin

COPY src ./src

ENV PYTHONPATH=/app

# Mirror the workspace pod's unprivileged identity (uid/gid 65532) so that
# when co-located in the workspace pod sharing the tenant PVC subPath, files
# soffice writes carry the ownership the main container expects (fsGroup
# 65532). See workspace Dockerfile + apps/workspace-operator podSpec
# securityContext.
RUN groupadd --system --gid 65532 tool \
 && useradd --system --uid 65532 --gid 65532 --home-dir /home/tool --shell /bin/bash tool \
 && mkdir -p /home/tool \
 && chown -R tool:tool /home/tool
ENV HOME=/home/tool

EXPOSE 8090

USER tool

ENTRYPOINT ["python", "-m", "src.server"]
