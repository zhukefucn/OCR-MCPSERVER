from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def test_mineru_request_is_immutable_and_has_no_transport_override(tmp_path: Path) -> None:
    from ocr_mcp_server.domain import MinerUParseRequest

    request = MinerUParseRequest(
        file_task_id="local-task",
        source_path=tmp_path / "source.pdf",
        upload_name="safe.pdf",
        output_directory=tmp_path / "results",
    )

    assert request.file_task_id == "local-task"
    assert request.source_path == tmp_path / "source.pdf"
    assert request.upload_name == "safe.pdf"
    assert request.output_directory == tmp_path / "results"
    assert not hasattr(request, "backend")
    assert not hasattr(request, "server_url")
    with pytest.raises(FrozenInstanceError):
        request.upload_name = "changed.pdf"  # type: ignore[misc]


def test_mineru_public_contracts_bind_local_task_context(tmp_path: Path) -> None:
    from ocr_mcp_server.domain import (
        MinerUDocumentResult,
        MinerUProgress,
        MinerUProgressStatus,
        MinerUSubmission,
    )

    submission = MinerUSubmission(
        file_task_id="local-task",
        upstream_task_id="upstream-task",
        status_url="https://api.example.test/tasks/upstream-task",
        result_url="https://api.example.test/tasks/upstream-task/result",
        queued_ahead=2,
    )
    progress = MinerUProgress(
        file_task_id="local-task",
        upstream_task_id="upstream-task",
        status=MinerUProgressStatus.PENDING,
        queued_ahead=2,
    )
    result = MinerUDocumentResult(
        file_task_id="local-task",
        upstream_task_id="upstream-task",
        result_root=tmp_path / "safe",
        markdown_path=tmp_path / "safe" / "parse" / "safe.md",
        middle_json_path=None,
        content_list_v2_path=tmp_path
        / "safe"
        / "parse"
        / "safe_content_list_v2.json",
        legacy_content_list_path=None,
        images_directory=tmp_path / "safe" / "parse" / "images",
    )

    assert submission.file_task_id == progress.file_task_id == result.file_task_id
    assert submission.queued_ahead == progress.queued_ahead == 2
    assert result.middle_json_path is None


@pytest.mark.parametrize(
    ("code_name", "machine_code", "retry_safe"),
    [
        ("UNAVAILABLE", "mineru_unavailable", True),
        ("AMBIGUOUS_SUBMISSION", "mineru_submission_ambiguous", False),
        ("DEADLINE_EXCEEDED", "mineru_deadline_exceeded", False),
        ("UPSTREAM_FAILURE", "mineru_upstream_failed", False),
        ("INVALID_RESPONSE", "mineru_response_invalid", False),
        ("UNSAFE_ARCHIVE", "mineru_archive_unsafe", False),
    ],
)
def test_mineru_failure_has_stable_safe_categories(
    code_name: str, machine_code: str, retry_safe: bool
) -> None:
    from ocr_mcp_server.domain import MinerUErrorCode, MinerUFailure

    secret = "private filename and recognized document text"
    code = getattr(MinerUErrorCode, code_name)
    failure = MinerUFailure(code, cause=RuntimeError(secret))

    assert failure.code == machine_code
    assert failure.retry_file_task_safe is retry_safe
    assert secret not in str(failure)
    assert secret not in repr(failure)
    assert secret not in repr(vars(failure))
