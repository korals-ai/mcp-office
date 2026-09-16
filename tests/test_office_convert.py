"""Tests for the office conversion logic.

The service is a local stub (``convert_stub`` in conftest) answering the same
``/cool/convert-to/<fmt>`` contract Collabora Online does, so what is pinned
here is the real wire call — the multipart field, the format path segment,
the bytes written back — plus every failure the agent must be able to read:
unsupported format, missing source, unreachable, non-2xx, empty answer,
timeout. Fidelity of the rendering itself is the service's, measured in the
platform's experiments, not something a unit test can assert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.office_convert import (
    SUPPORTED_FORMATS,
    OfficeConvertError,
    convert,
    convert_endpoint,
)
from tests.conftest import ConvertStub


def _doc(tmp_path: Path, name: str = "doc.docx", body: bytes = b"PK\x03\x04 fake docx") -> Path:
    src = tmp_path / name
    src.write_bytes(body)
    return src


def test_supported_formats_includes_core_roundtrips() -> None:
    for fmt in ("pdf", "docx", "xlsx", "pptx", "txt"):
        assert fmt in SUPPORTED_FORMATS


def test_endpoint_is_the_format_path_segment_under_cool() -> None:
    assert convert_endpoint("http://collabora:9980", "pdf") == (
        "http://collabora:9980/cool/convert-to/pdf"
    )
    # A trailing slash on the base must not double up.
    assert convert_endpoint("http://collabora:9980/", "docx").endswith("/cool/convert-to/docx")


def test_unsupported_format_rejected_before_touching_the_network(
    tmp_path: Path, convert_stub: ConvertStub
) -> None:
    src = _doc(tmp_path, "doc.rtf")
    with pytest.raises(OfficeConvertError, match="unsupported output format"):
        convert(src, tmp_path, to="xyz", convert_url=convert_stub.url)
    assert convert_stub.requests == []


def test_missing_source_raises(tmp_path: Path, convert_stub: ConvertStub) -> None:
    with pytest.raises(OfficeConvertError, match="source does not exist"):
        convert(tmp_path / "nope.docx", tmp_path, to="pdf", convert_url=convert_stub.url)
    assert convert_stub.requests == []


def test_directory_source_raises(tmp_path: Path, convert_stub: ConvertStub) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(OfficeConvertError, match="source is not a file"):
        convert(d, tmp_path, to="pdf", convert_url=convert_stub.url)


def test_posts_the_file_as_multipart_data_and_writes_the_answer(
    tmp_path: Path, convert_stub: ConvertStub
) -> None:
    src = _doc(tmp_path, "rfp.docx", b"PK\x03\x04 the document bytes")
    out_dir = tmp_path / "out"
    convert_stub.body = b"%PDF-1.7 rendered"

    out = convert(src, out_dir, to="PDF", convert_url=convert_stub.url)

    assert out == out_dir / "rfp.pdf"
    assert out.read_bytes() == b"%PDF-1.7 rendered"
    (req,) = convert_stub.requests
    assert req["path"] == "/cool/convert-to/pdf"
    assert str(req["content_type"]).startswith("multipart/form-data")
    body = req["body"]
    assert isinstance(body, bytes)
    # The multipart field the service reads is `data`, carrying the filename
    # (its extension is how the service picks the import filter).
    assert b'name="data"; filename="rfp.docx"' in body
    assert b"PK\x03\x04 the document bytes" in body


def test_output_extension_follows_the_format_not_the_source(
    tmp_path: Path, convert_stub: ConvertStub
) -> None:
    src = _doc(tmp_path, "sheet.xlsx")
    convert_stub.body = b"a,b\n1,2\n"
    out = convert(src, tmp_path, to="txt", convert_url=convert_stub.url)
    assert out.name == "sheet.txt"
    assert convert_stub.requests[0]["path"] == "/cool/convert-to/txt"


def test_non_2xx_surfaces_status_and_the_services_words(
    tmp_path: Path, convert_stub: ConvertStub
) -> None:
    src = _doc(tmp_path)
    convert_stub.status = 400
    convert_stub.body = b"Failed to convert the document."
    convert_stub.content_type = "text/plain"
    with pytest.raises(OfficeConvertError, match="answered 400") as info:
        convert(src, tmp_path, to="pdf", convert_url=convert_stub.url)
    assert info.value.stderr == "Failed to convert the document."
    assert not (tmp_path / "doc.pdf").exists()


def test_empty_200_is_a_failure_not_an_empty_file(
    tmp_path: Path, convert_stub: ConvertStub
) -> None:
    src = _doc(tmp_path)
    convert_stub.body = b""
    with pytest.raises(OfficeConvertError, match="produced no output"):
        convert(src, tmp_path, to="pdf", convert_url=convert_stub.url)
    assert not (tmp_path / "doc.pdf").exists()


def test_unreachable_service_names_the_endpoint(tmp_path: Path) -> None:
    src = _doc(tmp_path)
    # A closed port on loopback: connection refused, immediately.
    with pytest.raises(OfficeConvertError, match=r"unreachable at http://127\.0\.0\.1:9/cool"):
        convert(src, tmp_path, to="pdf", convert_url="http://127.0.0.1:9")


def test_timeout_is_bounded_and_reported(tmp_path: Path, convert_stub: ConvertStub) -> None:
    src = _doc(tmp_path)
    convert_stub.delay_s = 0.5
    with pytest.raises(OfficeConvertError, match="timed out after 0s"):
        convert(src, tmp_path, to="pdf", convert_url=convert_stub.url, timeout_s=0.1)
