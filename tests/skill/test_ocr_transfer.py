from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Thread
from types import SimpleNamespace
import warnings
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


def make_zip_from_entries(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return buffer.getvalue()


def patch_central_uncompressed_size(
    payload: bytes,
    declared_size: int,
    entry_index: int = 0,
) -> bytes:
    patched = bytearray(payload)
    offset = -1
    for _ in range(entry_index + 1):
        offset = patched.find(b"PK\x01\x02", offset + 1)
        assert offset >= 0
    patched[offset + 24 : offset + 28] = declared_size.to_bytes(4, "little")
    return bytes(patched)


def run_transfer(
    *args: str,
    api_key: str | None = "test-key",
    extra_env: dict[str, str] | None = None,
):
    env = os.environ.copy()
    if api_key is None:
        env.pop("OCR_MCP_API_KEY", None)
    else:
        env["OCR_MCP_API_KEY"] = api_key
    if extra_env is not None:
        env.update(extra_env)
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


def create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
        omit_content_length=False,
        declared_content_length=None,
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
            if not state.omit_content_length:
                content_length = state.declared_content_length
                if content_length is None:
                    content_length = len(body)
                self.send_header("Content-Length", str(content_length))
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


@pytest.fixture
def cross_origin_redirect_servers():
    state = SimpleNamespace(
        source_request_count=0,
        target_request_count=0,
        target_requests=[],
        source_body_token="source-redirect-body-secret",
        target_body_token="target-redirect-body-secret",
    )
    upload_receipt = json.dumps(
        {
            "file_id": "11111111-1111-4111-8111-111111111111",
            "size_bytes": 9,
            "media_type": "application/pdf",
            "unknown_body": state.target_body_token,
        }
    ).encode("utf-8")
    artifact_payload = make_zip(
        {
            "final.md": state.target_body_token.encode("utf-8"),
            "artifact_manifest.json": b"{}",
        }
    )

    class TargetHandler(BaseHTTPRequestHandler):
        def record_and_respond(self) -> None:
            state.target_request_count += 1
            state.target_requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "api_keys": self.headers.get_all("X-API-Key"),
                }
            )
            if self.path.startswith("/upload-target"):
                body = upload_receipt
                content_type = "application/json"
            else:
                body = artifact_payload
                content_type = "application/zip"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self.record_and_respond()

        def do_POST(self) -> None:
            self.record_and_respond()

        def log_message(self, format: str, *args: object) -> None:
            return

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = Thread(target=target_server.serve_forever, daemon=True)
    target_thread.start()
    target_base_url = f"http://127.0.0.1:{target_server.server_port}"

    class SourceHandler(BaseHTTPRequestHandler):
        def redirect(self, target_path: str) -> None:
            state.source_request_count += 1
            body = state.source_body_token.encode("utf-8")
            location = (
                f"{target_base_url}/{target_path}"
                "?opaque-location-token=do-not-log"
            )
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.redirect("upload-target")

        def do_GET(self) -> None:
            self.redirect("download-target")

        def log_message(self, format: str, *args: object) -> None:
            return

    source_server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source_thread = Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    state.source_base_url = f"http://127.0.0.1:{source_server.server_port}"
    state.location_token = "opaque-location-token=do-not-log"
    try:
        yield state
    finally:
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)
        target_server.shutdown()
        target_server.server_close()
        target_thread.join(timeout=5)


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


def test_upload_rejects_cross_origin_redirect_without_forwarding_key(
    tmp_path: Path, cross_origin_redirect_servers
) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-test")
    api_key = "redirect-secret-key"

    result, payload = run_transfer(
        "-Action",
        "upload",
        "-Path",
        str(source),
        "-BaseUrl",
        cross_origin_redirect_servers.source_base_url,
        api_key=api_key,
    )

    output = result.stdout + result.stderr
    assert cross_origin_redirect_servers.source_request_count == 1
    assert cross_origin_redirect_servers.target_request_count == 0
    assert result.returncode != 0
    assert payload["error"]["code"] == "upload_failed"
    assert api_key not in output
    assert cross_origin_redirect_servers.location_token not in output
    assert cross_origin_redirect_servers.source_body_token not in output
    assert cross_origin_redirect_servers.target_body_token not in output


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


def test_download_rejects_cross_origin_redirect_without_forwarding_key(
    tmp_path: Path, cross_origin_redirect_servers
) -> None:
    api_key = "redirect-secret-key"

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        cross_origin_redirect_servers.source_base_url,
        api_key=api_key,
    )

    output = result.stdout + result.stderr
    assert cross_origin_redirect_servers.source_request_count == 1
    assert cross_origin_redirect_servers.target_request_count == 0
    assert result.returncode != 0
    assert payload["error"]["code"] == "download_failed"
    assert api_key not in output
    assert cross_origin_redirect_servers.location_token not in output
    assert cross_origin_redirect_servers.source_body_token not in output
    assert cross_origin_redirect_servers.target_body_token not in output
    assert list(tmp_path.iterdir()) == []


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


def test_download_reuse_rebuilds_stale_extract_without_http(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"# current ZIP",
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
    extract_path = Path(first_payload["extract_path"])
    (extract_path / "final.md").write_text("# stale extract", encoding="utf-8")
    (extract_path / "stale-only.txt").write_text("stale", encoding="utf-8")
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
    assert artifact_server.request_count == 1
    assert (extract_path / "final.md").read_bytes() == b"# current ZIP"
    assert not (extract_path / "stale-only.txt").exists()


def test_download_reuse_replaces_junction_without_touching_external_target(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"# current ZIP",
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
    extract_path = Path(first_payload["extract_path"])
    shutil.rmtree(extract_path)
    external_path = tmp_path.parent / f"{tmp_path.name}-external"
    external_path.mkdir()
    external_marker = external_path / "marker.txt"
    external_marker.write_text("outside marker", encoding="utf-8")
    (external_path / "final.md").write_text("# outside", encoding="utf-8")
    create_junction(extract_path, external_path)
    assert os.path.samefile(extract_path, external_path)
    artifact_server.fail_if_requested = True

    try:
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
        assert artifact_server.request_count == 1
        assert not os.path.samefile(extract_path, external_path)
        assert (extract_path / "final.md").read_bytes() == b"# current ZIP"
        assert external_marker.read_text(encoding="utf-8") == "outside marker"
        assert (external_path / "final.md").read_text(encoding="utf-8") == "# outside"
    finally:
        if extract_path.exists():
            try:
                if os.path.samefile(extract_path, external_path):
                    os.rmdir(extract_path)
            except FileNotFoundError:
                pass
        shutil.rmtree(external_path, ignore_errors=True)


def test_download_rejects_content_length_over_configured_limit(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"# result",
            "artifact_manifest.json": b"{}",
        }
    )
    assert len(artifact_server.payload) > 64

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
        extra_env={"OCR_MCP_MAX_DOWNLOAD_BYTES": "64"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "download_failed"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_stream_over_limit_without_content_length(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = b"x" * 65
    artifact_server.omit_content_length = True

    result, payload = run_transfer(
        "-Action",
        "download",
        "-ArtifactId",
        ARTIFACT_ID,
        "-OutputRoot",
        str(tmp_path),
        "-BaseUrl",
        artifact_server.base_url,
        extra_env={"OCR_MCP_MAX_DOWNLOAD_BYTES": "64"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "download_failed"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_declared_entry_size_over_limit(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = patch_central_uncompressed_size(
        make_zip(
            {
                "final.md": b"small",
                "artifact_manifest.json": b"{}",
            }
        ),
        declared_size=17,
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
        extra_env={"OCR_MCP_MAX_ENTRY_BYTES": "16"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_actual_entry_stream_over_limit(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = patch_central_uncompressed_size(
        make_zip(
            {
                "final.md": b"x" * 32,
                "artifact_manifest.json": b"{}",
            }
        ),
        declared_size=8,
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
        extra_env={"OCR_MCP_MAX_ENTRY_BYTES": "16"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_total_extracted_bytes_over_limit(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip(
        {
            "final.md": b"12345678",
            "artifact_manifest.json": b"12345678",
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
        extra_env={"OCR_MCP_MAX_EXTRACTED_BYTES": "12"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_entry_count_over_limit(
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
        extra_env={"OCR_MCP_MAX_ARCHIVE_ENTRIES": "1"},
    )

    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "unsafe_entries",
    [
        [
            ("final.md", b"# first"),
            ("final.md", b"# duplicate"),
        ],
        [
            ("final.md", b"# result"),
            ("FINAL.MD", b"# case conflict"),
        ],
        [
            ("final.md", b"# result"),
            ("folder/", b""),
            ("folder", b"file conflict"),
        ],
    ],
)
def test_download_rejects_conflicting_targets_without_poisoning_cache(
    tmp_path: Path,
    artifact_server,
    unsafe_entries: list[tuple[str, bytes]],
) -> None:
    artifact_server.payload = make_zip_from_entries(unsafe_entries)

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

    assert first_result.returncode != 0
    assert first_payload["error"]["code"] == "invalid_artifact"
    assert list(tmp_path.iterdir()) == []
    assert artifact_server.request_count == 1

    artifact_server.payload = make_zip(
        {
            "final.md": b"# recovered",
            "artifact_manifest.json": b"{}",
        }
    )
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
    assert second_payload["reused"] is False
    assert artifact_server.request_count == 2
    assert (
        Path(second_payload["extract_path"]) / "final.md"
    ).read_bytes() == b"# recovered"
