from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "skills" / "using-ocr-mcpserver" / "scripts" / "ocr-transfer.ps1"


def run_transfer(*args: str, api_key: str | None = "test-key"):
    env = os.environ.copy()
    if api_key is None:
        env.pop("OCR_MCP_API_KEY", None)
    else:
        env["OCR_MCP_API_KEY"] = api_key
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result, payload


def test_missing_api_key_fails_without_echoing_secrets(tmp_path: Path) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-test")
    result, payload = run_transfer(
        "-Action", "upload", "-Path", str(source), api_key=None
    )
    assert result.returncode != 0
    assert payload == {
        "ok": False,
        "error": {
            "code": "authentication_unavailable",
            "message": "OCR authentication is not configured.",
        },
    }


@pytest.fixture
def upload_server():
    received: dict[str, object] = {}

    class UploadHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received["path"] = self.path
            received["api_keys"] = self.headers.get_all("X-API-Key")
            received["document_names"] = self.headers.get_all("X-Document-Name")
            received["content_type"] = self.headers.get("Content-Type")
            remaining = int(self.headers["Content-Length"])
            received["content_length"] = remaining
            digest = hashlib.sha256()
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            received["sha256"] = digest.hexdigest()
            body = json.dumps(
                {
                    "file_id": "11111111-1111-4111-8111-111111111111",
                    "size_bytes": received["content_length"],
                    "media_type": received["content_type"],
                }
            ).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_port}",
            received=received,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("not OCR input", encoding="utf-8")
    result, payload = run_transfer("-Action", "upload", "-Path", str(source))
    assert result.returncode != 0
    assert payload["error"]["code"] == "unsupported_media_type"


def test_upload_rejects_file_over_60_mib(tmp_path: Path) -> None:
    source = tmp_path / "large.pdf"
    with source.open("wb") as stream:
        stream.truncate(62_914_561)
    result, payload = run_transfer("-Action", "upload", "-Path", str(source))
    assert result.returncode != 0
    assert payload["error"]["code"] == "file_too_large"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("document.pdf", "application/pdf"),
        ("document.png", "image/png"),
        ("document.jpg", "image/jpeg"),
        ("document.jpeg", "image/jpeg"),
    ],
)
def test_upload_streams_file_and_returns_safe_receipt(
    tmp_path: Path, upload_server, name: str, content_type: str
) -> None:
    source = tmp_path / name
    source.write_bytes(b"%PDF-test")
    result, payload = run_transfer(
        "-Action",
        "upload",
        "-Path",
        str(source),
        "-BaseUrl",
        upload_server.base_url,
    )
    assert result.returncode == 0
    assert payload == {
        "ok": True,
        "action": "upload",
        "file_id": "11111111-1111-4111-8111-111111111111",
        "size_bytes": 9,
        "media_type": content_type,
    }
    assert upload_server.received == {
        "path": "/v1/uploads",
        "api_keys": ["test-key"],
        "document_names": [name],
        "content_type": content_type,
        "content_length": 9,
        "sha256": hashlib.sha256(b"%PDF-test").hexdigest(),
    }
    assert "test-key" not in result.stdout + result.stderr
