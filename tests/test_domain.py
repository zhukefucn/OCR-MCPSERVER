from __future__ import annotations

from datetime import UTC
from uuid import UUID

from ocr_mcp_server.domain.constants import (
    DEFAULT_AUDIT_METADATA_RETENTION_DAYS,
    DEFAULT_INPUT_RETENTION_HOURS,
    DEFAULT_INTERMEDIATE_RETENTION_HOURS,
    DEFAULT_MAX_BATCH_SIZE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_FILE_SIZE_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_RESULT_RETENTION_HOURS,
    SUPPORTED_EXTENSIONS,
)
from ocr_mcp_server.domain.errors import (
    ConfigurationError,
    InputValidationError,
    LeaseConflictError,
    PersistenceError,
    StateTransitionError,
)
from ocr_mcp_server.domain.models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    SecondaryOCREngine,
    new_id,
    utc_now,
)


def test_default_constraints_match_the_service_contract() -> None:
    assert SUPPORTED_EXTENSIONS == frozenset({".pdf", ".png", ".jpg", ".jpeg"})
    assert DEFAULT_MAX_FILES == 20
    assert DEFAULT_MAX_FILE_SIZE_BYTES == 30 * 1024 * 1024
    assert DEFAULT_MAX_PAGES == 500
    assert DEFAULT_MAX_BATCH_SIZE_BYTES == 1024**3
    assert DEFAULT_INPUT_RETENTION_HOURS == 24
    assert DEFAULT_INTERMEDIATE_RETENTION_HOURS == 24
    assert DEFAULT_RESULT_RETENTION_HOURS == 24
    assert DEFAULT_AUDIT_METADATA_RETENTION_DAYS == 30


def test_domain_enums_include_the_approved_values() -> None:
    assert {status.value for status in BatchStatus} >= {
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
    }
    assert {status.value for status in FileStatus} >= {
        "completed",
        "completed_with_warnings",
        "failed",
        "cancelled",
    }
    assert {stage.value for stage in ProcessingStage} >= {
        "uploading",
        "validating",
        "queued",
        "mineru_parsing",
        "collecting_images",
        "detecting_orientation",
        "classifying_images",
        "recognizing_images",
        "merging",
        "packaging",
        "publishing",
        "completed",
        "completed_with_warnings",
        "completed_with_errors",
        "failed",
        "cancelled",
    }
    assert {engine.value for engine in SecondaryOCREngine} == {
        "pp_structure_v3",
        "paddleocr_vl",
    }


def test_new_id_returns_a_uuid_string() -> None:
    value = new_id()

    assert str(UUID(value)) == value


def test_utc_now_returns_an_aware_utc_datetime() -> None:
    value = utc_now()

    assert value.tzinfo is UTC
    assert value.utcoffset().total_seconds() == 0


def test_domain_errors_expose_stable_code_and_safe_message_only() -> None:
    sensitive_cause = RuntimeError("recognized private business text")

    config_error = ConfigurationError(cause=sensitive_cause)
    input_error = InputValidationError(cause=sensitive_cause)
    state_error = StateTransitionError(cause=sensitive_cause)
    lease_error = LeaseConflictError(cause=sensitive_cause)
    persistence_error = PersistenceError(cause=sensitive_cause)

    assert config_error.code == "configuration_invalid"
    assert str(config_error) == "Service configuration is invalid."
    assert input_error.code == "input_invalid"
    assert str(input_error) == "Input validation failed."
    assert "recognized private business text" not in str(config_error)
    assert "recognized private business text" not in str(input_error)
    assert state_error.code == "state_transition_invalid"
    assert str(state_error) == "Task state transition is invalid."
    assert lease_error.code == "lease_conflict"
    assert str(lease_error) == "Task lease is invalid or expired."
    assert persistence_error.code == "persistence_error"
    assert str(persistence_error) == "Task persistence operation failed."
