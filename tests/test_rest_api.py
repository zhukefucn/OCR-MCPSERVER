from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from ocr_mcp_server.api.contracts import (
    ArtifactReference,
    BatchStatusResponse,
    FileStatusResponse,
    OrientationReparseSubmission,
    ParseSubmission,
    UploadReceipt,
)
from ocr_mcp_server.api.gateway import (
    GatewayCapacityExceeded,
    GatewayConflict,
    GatewayFailure,
    GatewayNotFound,
    GatewayUnavailable,
)
from ocr_mcp_server.app import create_app
from ocr_mcp_server.settings import AppSettings


KEY = "a-secure-api-key-0000000000000001"
AUTH = {"X-API-Key": KEY}


def completed_status(batch_id: str) -> BatchStatusResponse:
    file_id = str(uuid4())
    return BatchStatusResponse(
        batch_id=batch_id,
        status="completed",
        progress=100,
        total_files=1,
        completed_files=1,
        failed_files=0,
        files=[
            FileStatusResponse(
                file_id=file_id,
                status="completed",
                stage="completed",
                progress=100,
            )
        ],
        artifacts=[
            ArtifactReference(
                artifact_id=str(uuid4()),
                download_url=f"https://api.example.test/v1/tasks/{batch_id}/artifact",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        ],
    )


@dataclass
class FakeGateway:
    upload_chunks: list[bytes] = field(default_factory=list)
    parse_request: object | None = None
    status_id: str | None = None
    reparse_request: object | None = None
    failure: Exception | None = None
    upload_display_name: str | None = None
    upload_idempotency_key: str | None = None

    async def upload_document(
        self,
        content: AsyncIterable[bytes],
        *,
        display_name: str,
        media_type: str,
        content_length: int | None,
        idempotency_key: str | None,
    ) -> UploadReceipt:
        assert not isinstance(content, (bytes, bytearray))
        self.upload_display_name = display_name
        self.upload_idempotency_key = idempotency_key
        async for chunk in content:
            self.upload_chunks.append(chunk)
        if self.failure:
            raise self.failure
        return UploadReceipt(
            file_id=str(uuid4()),
            size_bytes=sum(map(len, self.upload_chunks)),
            media_type=media_type,
        )

    async def parse_documents(self, request, *, progress=None) -> ParseSubmission:
        self.parse_request = request
        if self.failure:
            raise self.failure
        return ParseSubmission(batch_id=str(uuid4()), status="queued")

    async def get_task_status(self, batch_id: str) -> BatchStatusResponse:
        self.status_id = batch_id
        if self.failure:
            raise self.failure
        return completed_status(batch_id)

    async def reparse_with_page_orientation(
        self, request, *, progress=None
    ) -> OrientationReparseSubmission:
        self.reparse_request = request
        if self.failure:
            raise self.failure
        return OrientationReparseSubmission(batch_id=str(uuid4()), status="queued")


def client(gateway: object, *, max_size: int = 30 * 1024 * 1024) -> TestClient:
    settings = AppSettings(
        auth={"api_keys": [KEY]}, limits={"max_file_size_bytes": max_size}
    )
    return TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False)


def test_binary_upload_streams_to_gateway_and_returns_receipt() -> None:
    gateway = FakeGateway()
    with client(gateway) as api:
        response = api.post(
            "/v1/uploads",
            headers={
                **AUTH,
                "Content-Type": "application/pdf",
                "X-Document-Name": "statement.pdf",
                "Idempotency-Key": "upload-1",
            },
            content=b"%PDF-safe-test",
        )
    assert response.status_code == 201
    assert b"".join(gateway.upload_chunks) == b"%PDF-safe-test"
    assert gateway.upload_display_name == "statement.pdf"
    assert gateway.upload_idempotency_key == "upload-1"
    assert response.json()["media_type"] == "application/pdf"


def test_upload_rejects_unsupported_empty_and_oversized_bodies_before_gateway() -> None:
    for content, content_type, max_size, expected in (
        (b"value", "text/plain", 100, 415),
        (b"", "application/pdf", 100, 422),
        (b"too-large", "application/pdf", 3, 413),
    ):
        gateway = FakeGateway()
        with client(gateway, max_size=max_size) as api:
            response = api.post(
                "/v1/uploads",
                headers={
                    **AUTH,
                    "Content-Type": content_type,
                    "X-Document-Name": "statement.pdf",
                },
                content=content,
            )
        assert response.status_code == expected
        assert response.json()["error"]["code"] in {
            "unsupported_media_type",
            "invalid_request",
            "capacity_exceeded",
        }


def test_upload_rejects_missing_or_path_like_display_names() -> None:
    for display_name in (
        None,
        "C:/private/customer.pdf",
        "../customer.pdf",
        "folder\\customer.pdf",
    ):
        gateway = FakeGateway()
        headers = {**AUTH, "Content-Type": "application/pdf"}
        if display_name is not None:
            headers["X-Document-Name"] = display_name
        with client(gateway) as api:
            response = api.post("/v1/uploads", headers=headers, content=b"%PDF")
        assert response.status_code == 422
        assert gateway.upload_chunks == []


def test_rest_workflow_uses_strict_contracts_and_explicit_operation_ids() -> None:
    gateway = FakeGateway()
    file_id = str(uuid4())
    with client(gateway) as api:
        parse = api.post(
            "/v1/tasks",
            headers=AUTH,
            json={"sources": [{"file_id": file_id}], "idempotency_key": "request-1"},
        )
        batch_id = parse.json()["batch_id"]
        status = api.get(f"/v1/tasks/{batch_id}", headers=AUTH)
        reparse = api.post(
            "/v1/orientation-reparse",
            headers=AUTH,
            json={"recovery_token": "opaque-token_123", "pages": [2, 4]},
        )
        schema = api.get("/openapi.json", headers=AUTH).json()
    assert parse.status_code == 202
    assert status.status_code == 200
    assert reparse.status_code == 202
    assert gateway.parse_request.sources[0].file_id == file_id
    assert gateway.status_id == batch_id
    assert gateway.reparse_request.pages == [2, 4]
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert {
        "uploadDocument",
        "parseDocuments",
        "getTaskStatus",
        "reparseWithPageOrientation",
        "liveness",
    } <= operation_ids
    serialized_schema = str(schema)
    for forbidden in ("engine", "backend", "device", "angle", "server_url"):
        assert forbidden not in serialized_schema


def test_validation_errors_are_content_free_and_do_not_reach_gateway() -> None:
    sensitive = "https://files.example.test/customer-secret-name.pdf"
    gateway = FakeGateway()
    with client(gateway) as api:
        response = api.post(
            "/v1/tasks",
            headers=AUTH,
            json={"sources": [{"url": sensitive, "path": "C:/private/customer.pdf"}]},
        )
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "The request is invalid."}
    }
    assert sensitive not in response.text
    assert gateway.parse_request is None


def test_task_status_rejects_noncanonical_identifier_before_gateway() -> None:
    gateway = FakeGateway()
    with client(gateway) as api:
        response = api.get("/v1/tasks/not-a-uuid", headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert gateway.status_id is None


def test_gateway_failures_map_to_stable_safe_http_errors() -> None:
    cases = [
        (GatewayNotFound(), 404, "not_found"),
        (GatewayConflict(), 409, "conflict"),
        (GatewayCapacityExceeded(), 429, "capacity_exceeded"),
        (GatewayUnavailable(), 503, "service_unavailable"),
        (GatewayFailure(), 500, "internal_error"),
        (RuntimeError("recognized private business text"), 500, "internal_error"),
    ]
    for failure, expected_status, expected_code in cases:
        with client(FakeGateway(failure=failure)) as api:
            response = api.get(f"/v1/tasks/{uuid4()}", headers=AUTH)
        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == expected_code
        assert "recognized private business text" not in response.text
