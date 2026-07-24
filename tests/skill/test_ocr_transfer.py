from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "skills" / "using-ocr-mcpserver" / "scripts" / "ocr-transfer.ps1"
ARTIFACT_ID = "22222222-2222-4222-8222-222222222222"


def make_zip(
    entries: dict[str, bytes],
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    payload = buffer.getvalue()
    for name in entries:
        normalized_name = name.replace("\\", "/")
        if normalized_name != name:
            normalized_bytes = normalized_name.encode("utf-8")
            central_name_offset = payload.rfind(normalized_bytes)
            payload = (
                payload[:central_name_offset]
                + name.encode("utf-8")
                + payload[central_name_offset + len(normalized_bytes) :]
            )
    return payload


def make_crc_damaged_zip() -> bytes:
    marker = b"unique stored final markdown bytes"
    payload = bytearray(
        make_zip(
            {
                "final.md": marker,
                "artifact_manifest.json": b"{}",
            },
            compression=zipfile.ZIP_STORED,
        )
    )
    marker_offset = payload.index(marker)
    payload[marker_offset] ^= 0xFF
    return bytes(payload)


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


@pytest.fixture
def artifact_server():
    state = SimpleNamespace(
        payload=b"",
        request_count=0,
        received=[],
        fail_if_requested=False,
    )

    class ArtifactHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.request_count += 1
            state.received.append(
                {
                    "path": self.path,
                    "api_keys": self.headers.get_all("X-API-Key"),
                }
            )
            if state.fail_if_requested:
                body = b"must not be requested"
                self.send_response(503)
            else:
                body = state.payload
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ArtifactHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
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


def test_download_keeps_zip_and_extracts_final_markdown(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"# result",
            "artifact_manifest.json": b"{}",
        }
    )

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode == 0
    zip_path = Path(payload["zip_path"])
    extract_path = Path(payload["extract_path"])
    assert zip_path.read_bytes() == artifact_server.payload
    assert (extract_path / "final.md").read_bytes() == b"# result"
    assert payload == {
        "ok": True,
        "action": "download",
        "artifact_id": ARTIFACT_ID,
        "zip_path": str(zip_path),
        "extract_path": str(extract_path),
        "entry_count": 2,
        "reused": False,
    }
    assert artifact_server.received == [
        {
            "path": f"/v1/artifacts/{ARTIFACT_ID}",
            "api_keys": ["test-key"],
        }
    ]


def test_download_requires_exact_root_final_markdown(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "nested/final.md": b"# misplaced",
            "artifact_manifest.json": b"{}",
        }
    )

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


def test_download_reads_entries_to_eof_and_rejects_crc_damage(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_crc_damaged_zip()

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_malformed_zip(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = b"not a ZIP archive"

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "archive_name",
    [
        "../escape.txt",
        r"nested\escape.txt",
        "safe/../../normalized-escape.txt",
    ],
)
def test_download_rejects_unsafe_archive_paths(
    tmp_path: Path, artifact_server, archive_name: str
) -> None:
    artifact_server.payload = make_zip(
        {
            archive_name: b"unsafe",
            "final.md": b"# result",
        }
    )

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "unsafe_archive"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_absolute_archive_path(
    tmp_path: Path, artifact_server
) -> None:
    escaped_path = tmp_path.parent / "absolute-escape.txt"
    artifact_server.payload = make_zip(
        {
            escaped_path.as_posix(): b"unsafe",
            "final.md": b"# result",
        }
    )

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "unsafe_archive"
    assert not escaped_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_noncanonical_artifact_id_without_http(
    tmp_path: Path, artifact_server
) -> None:
    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID.replace("-", ""),
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert artifact_server.request_count == 0
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_download_reuses_valid_zip_and_extract_without_http(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"# result",
            "artifact_manifest.json": b"{}",
        }
    )
    first_result, first_payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )
    assert first_result.returncode == 0
    assert first_payload["reused"] is False
    assert artifact_server.request_count == 1
    artifact_server.fail_if_requested = True

    second_result, second_payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
    )

    assert second_result.returncode == 0
    assert second_payload["reused"] is True
    assert second_payload["entry_count"] == 2
    assert second_payload["zip_path"] == first_payload["zip_path"]
    assert second_payload["extract_path"] == first_payload["extract_path"]
    assert artifact_server.request_count == 1
