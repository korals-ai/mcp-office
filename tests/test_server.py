"""Tests for the MCP server wiring.

Verifies the server constructs and registers the ``convert`` tool, and that
the tool maps conversion failures to an MCP error rather than crashing. Does
not exercise a real ``soffice`` (absent in CI) — that's the integration
suite's job against the running sidecar.
"""

from __future__ import annotations

import pytest

from src import server


@pytest.mark.asyncio
async def test_convert_tool_is_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "convert" in names


@pytest.mark.asyncio
async def test_pdf_to_images_tool_is_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "pdf_to_images" in names


@pytest.mark.asyncio
async def test_pdf_to_images_tool_describes_src_and_dpi() -> None:
    tools = await server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "pdf_to_images")
    schema = tool.inputSchema
    assert "src" in schema["properties"]
    assert "dpi" in schema["properties"]
    assert "src" in schema.get("required", [])


@pytest.mark.asyncio
async def test_pdf_extract_text_tool_is_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "pdf_extract_text" in names


@pytest.mark.parametrize("name", ["author_xlsx", "author_docx", "author_pptx", "author_pdf"])
@pytest.mark.asyncio
async def test_author_tools_are_registered(name: str) -> None:
    tools = await server.mcp.list_tools()
    assert name in {t.name for t in tools}


@pytest.mark.asyncio
async def test_author_xlsx_tool_requires_path_and_rows() -> None:
    tools = await server.mcp.list_tools()
    schema = next(t for t in tools if t.name == "author_xlsx").inputSchema
    assert "path" in schema["properties"]
    assert "rows" in schema["properties"]
    required = set(schema.get("required", []))
    assert {"path", "rows"} <= required


@pytest.mark.asyncio
async def test_pdf_extract_text_tool_describes_src() -> None:
    tools = await server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "pdf_extract_text")
    schema = tool.inputSchema
    assert "src" in schema["properties"]
    assert "src" in schema.get("required", [])


@pytest.mark.asyncio
async def test_convert_tool_describes_src_and_to() -> None:
    tools = await server.mcp.list_tools()
    convert_tool = next(t for t in tools if t.name == "convert")
    schema = convert_tool.inputSchema
    assert "src" in schema["properties"]
    assert "to" in schema["properties"]
    # src is required, to has a default and is optional.
    assert "src" in schema.get("required", [])


def test_convert_missing_source_raises(tmp_path) -> None:
    # The tool wrapper delegates to office_convert and lets the error
    # propagate (FastMCP renders it as an MCP tool error).
    from src.office_convert import OfficeConvertError

    with pytest.raises(OfficeConvertError):
        server.convert(str(tmp_path / "absent.docx"), "pdf")


def test_pdf_to_images_missing_source_raises(tmp_path) -> None:
    from src.pdf_rasterize import PdfRasterizeError

    with pytest.raises(PdfRasterizeError):
        server.pdf_to_images(str(tmp_path / "absent.pdf"))


def test_pdf_extract_text_missing_source_raises(tmp_path) -> None:
    from src.pdf_text import PdfTextExtractError

    with pytest.raises(PdfTextExtractError):
        server.pdf_extract_text(str(tmp_path / "absent.pdf"))


@pytest.mark.asyncio
async def test_xlsx_extract_cells_tool_is_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "xlsx_extract_cells" in names


@pytest.mark.asyncio
async def test_xlsx_extract_cells_tool_describes_src_and_sheet() -> None:
    tools = await server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "xlsx_extract_cells")
    schema = tool.inputSchema
    assert "src" in schema["properties"]
    assert "sheet" in schema["properties"]
    assert "src" in schema.get("required", [])


def test_xlsx_extract_cells_missing_source_raises(tmp_path) -> None:
    from src.xlsx_extract import XlsxExtractError

    with pytest.raises(XlsxExtractError):
        server.xlsx_extract_cells(str(tmp_path / "absent.xlsx"))


def test_xlsx_extract_cells_reads_a_real_workbook(tmp_path) -> None:
    from openpyxl import Workbook

    dest = tmp_path / "book.xlsx"
    wb = Workbook()
    wb.active.append(["a", "b"])
    wb.save(dest)

    result = server.xlsx_extract_cells(str(dest))
    assert result["Sheet"]["rows"] == [["a", "b"]]
