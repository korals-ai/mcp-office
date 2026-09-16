"""Shared fixtures.

``convert_stub`` is a real HTTP server standing in for the office service's
``/cool/convert-to/<fmt>`` — so ``convert`` is exercised over the exact client
call production makes (multipart POST via httpx), not a mocked function. Tests
script its answer per call and read back what it received.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@dataclass
class ConvertStub:
    url: str
    status: int = 200
    body: bytes = b"%PDF-1.4 stub"
    content_type: str = "application/pdf"
    delay_s: float = 0.0
    requests: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture
def convert_stub() -> Iterator[ConvertStub]:
    state = ConvertStub(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            state.requests.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": raw,
                }
            )
            if state.delay_s:
                threading.Event().wait(state.delay_s)
            self.send_response(state.status)
            self.send_header("Content-Type", state.content_type)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
