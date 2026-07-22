from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ocr_mcp_server.api.contracts import (
    ArtifactReference,
    BatchStatusResponse,
    DocumentSource,
    FileStatusResponse,
    OrientationReparseRequest,
    ParseDocumentsRequest,
)


def _uuid() -> str:
    return str(uuid4())


def test_document_source_accepts_exactly_one_supported_source() -> None:
    assert DocumentSource(file_id=_uuid()).file_id is not None
    assert str(DocumentSource(url="https://files.example.test/a.pdf").url).startswith(
        "https://"
    )

    with pytest.raises(ValidationError):
        DocumentSource()
    with pytest.raises(ValidationError):
        DocumentSource(file_id=_uuid(), url="https://files.example.test/a.pdf")


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "C:/private/document.pdf"},
        {"file_id": "NOT-A-UUID"},
        {"url": "http://files.example.test/a.pdf"},
        {"url": "file:///private/document.pdf"},
        {"url": "https://user:secret@files.example.test/a.pdf"},
        {"url": "https://files.example.test/a.pdf#fragment"},
    ],
)
def test_document_source_rejects_paths_noncanonical_ids_and_unsafe_urls(
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        DocumentSource.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["engine", "backend", "model", "device", "angle", "concurrency", "server_url"],
)
def test_parse_contract_forbids_internal_controls(field: str) -> None:
    with pytest.raises(ValidationError):
        ParseDocumentsRequest.model_validate(
            {"sources": [{"file_id": _uuid()}], field: "internal-value"}
        )


def test_parse_contract_bounds_sources_and_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        ParseDocumentsRequest(sources=[])
    with pytest.raises(ValidationError):
        ParseDocumentsRequest(sources=[{"file_id": _uuid()}] * 21)
    with pytest.raises(ValidationError):
        ParseDocumentsRequest(
            sources=[{"file_id": _uuid()}], idempotency_key="x" * 129
        )
    with pytest.raises(ValidationError):
        ParseDocumentsRequest(
            sources=[{"file_id": _uuid()}], idempotency_key="contains\nnewline"
        )


def test_reparse_accepts_only_token_and_unique_positive_pages() -> None:
    request = OrientationReparseRequest(
        recovery_token="opaque-token_123", pages=[3, 1]
    )
    assert request.pages == [3, 1]

    for payload in (
        {"recovery_token": "opaque-token_123", "pages": [1, 1]},
        {"recovery_token": "opaque-token_123", "pages": [0]},
        {"recovery_token": "opaque-token_123", "angle": 90},
        {"recovery_token": "opaque-token_123", "engine": "paddle"},
    ):
        with pytest.raises(ValidationError):
            OrientationReparseRequest.model_validate(payload)

    for page in (True, 1.0, "1"):
        with pytest.raises(ValidationError):
            OrientationReparseRequest(
                recovery_token="opaque-token_123", pages=[page]
            )


def test_terminal_status_requires_consistent_counts_and_artifacts() -> None:
    batch_id = _uuid()
    file_id = _uuid()
    artifact = ArtifactReference(
        artifact_id=_uuid(),
        download_url=f"https://api.example.test/v1/tasks/{batch_id}/artifacts/result.zip",
        expires_at="2030-01-01T00:00:00Z",
    )
    response = BatchStatusResponse(
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
        artifacts=[artifact],
    )
    assert response.artifacts == [artifact]

    with pytest.raises(ValidationError):
        BatchStatusResponse(
            batch_id=batch_id,
            status="running",
            progress=100,
            total_files=1,
            completed_files=1,
            failed_files=0,
            files=[],
            artifacts=[artifact],
        )
    with pytest.raises(ValidationError):
        BatchStatusResponse(
            batch_id=batch_id,
            status="completed",
            progress=99,
            total_files=1,
            completed_files=1,
            failed_files=0,
            files=[],
            artifacts=[],
        )
