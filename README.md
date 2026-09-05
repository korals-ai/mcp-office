# mcp-office

Convert, rasterize and author Office documents. An [MCP](https://modelcontextprotocol.io) server speaking Streamable
HTTP: run it in a container, point your agent at `http://localhost:8090/mcp`.

LibreOffice document conversion (DOCX/XLSX/PPTX/ODF/PDF/HTML/TXT round-trips), PDF page rasterization and text extraction, structured Excel cell reads, and authoring of new DOCX/XLSX/PPTX/PDF files from structured input.

## Quickstart

```bash
docker compose up          # builds the image the first time
```

Then register it with your agent. Claude Code:

```bash
claude mcp add --transport http office http://localhost:8090/mcp
```

…or in a client config:

```json
{"mcpServers": {"office": {"type": "http", "url": "http://localhost:8090/mcp"}}}
```

## How files reach the tools

These tools take **paths, not uploads** — the agent names a file, the server
opens it in place and writes results back. Nothing but the path and the verdict
crosses the MCP wire, so a 200 MB file costs no tokens.

That means the container has to be able to see your files. `docker compose up`
mounts the directory you ran it from at `/work`, so tell the agent about
`/work/drawing.dxf`, not `~/drawing.dxf`. Mount somewhere else with
`WORKDIR=/path/to/project docker compose up`.

Files the tools create are written as your host user, not root:

```bash
MCP_UID=$(id -u) MCP_GID=$(id -g) docker compose up   # if your uid is not 1000
```

## Tools

- `convert`
- `pdf_to_images`
- `pdf_extract_text`
- `xlsx_extract_cells`
- `author_xlsx`
- `author_docx`
- `author_pptx`
- `author_pdf`
- `office_shell` — shell escape hatch for whatever the above don't cover.

Each tool's own description and typed signature — what the agent actually reads
to decide when to call it — is in `src/server.py`.

## Requirements

LibreOffice and poppler-utils, installed in the image. The build pulls ~400 MB of packages and takes several minutes the first time.

## Contributing

Issues and PRs are welcome and read directly.

One thing to know before you send a PR: this repository is a **one-way mirror**
of a directory in a private monorepo, which stays canonical. Contributions are
applied there and reappear here on the next sync, so your change lands with your
authorship upstream but arrives in this repo's history inside a sync commit.
Nothing here is force-pushed away, but don't expect your PR to be merged with a
green button.

## License

Apache-2.0 — see [LICENSE](LICENSE).
