from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ocr_mcp_server.domain.orientation import (
    OrientationDecision,
    OrientationEvidence,
    RecoveryClaim,
    RecoverySnapshot,
    RecoveryState,
    RecoveryTokenBinding,
    RecoveryTokenIssue,
)
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle


NOW = datetime(2026, 7, 22, tzinfo=UTC)


def binding(**overrides) -> RecoveryTokenBinding:
    values = {
        "file_id": "file-123",
        "batch_id": "11111111-1111-4111-8111-111111111111",
        "source_result_version": 2,
        "page_count": 5,
        "suspected_pages": (4, 2),
        "expires_at": NOW + timedelta(hours=12),
    }
    values.update(overrides)
    return RecoveryTokenBinding(**values)


def test_token_binding_canonicalizes_pages_and_validates_content_free_identity() -> None:
    value = binding()
    assert value.suspected_pages == (2, 4)

    for changes in (
        {"file_id": "client.pdf"},
        {"batch_id": "not-a-uuid"},
        {"source_result_version": True},
        {"page_count": 0},
        {"suspected_pages": (1, 1)},
        {"suspected_pages": (6,)},
        {"expires_at": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(ValueError):
            binding(**changes)


def test_evidence_and_decision_accept_only_finite_orthogonal_content_free_values() -> None:
    evidence = OrientationEvidence(
        page_number=2,
        angle=OrthogonalAngle.DEG_90,
        confidence=0.98,
        evidence_code="pdf_rotation_metadata",
    )
    decision = OrientationDecision.from_evidence(evidence, credible=True)
    assert decision.page_number == 2
    assert decision.angle is OrthogonalAngle.DEG_90
    assert decision.credible is True

    invalid = (
        {"page_number": 0},
        {"angle": 45},
        {"confidence": float("nan")},
        {"confidence": 1.01},
        {"evidence_code": "client.pdf"},
    )
    for changes in invalid:
        values = {
            "page_number": 1,
            "angle": OrthogonalAngle.DEG_0,
            "confidence": 0.5,
            "evidence_code": "metadata",
        }
        values.update(changes)
        with pytest.raises(ValueError):
            OrientationEvidence(**values)


def test_raw_secrets_are_redacted_from_issue_and_claim_representations() -> None:
    snapshot = RecoverySnapshot(
        file_id="file-123",
        batch_id="11111111-1111-4111-8111-111111111111",
        source_result_version=2,
        page_count=5,
        suspected_pages=(2, 4),
        selected_pages=None,
        expires_at=NOW + timedelta(hours=1),
        state=RecoveryState.ISSUED,
        request_fingerprint=None,
        claim_id=None,
        corrected_input_version=None,
        result_batch_id=None,
        result_version=None,
        error_code=None,
        version=1,
    )
    raw_token = "raw-recovery-secret-that-is-long-enough"
    issue = RecoveryTokenIssue(token=raw_token, snapshot=snapshot)
    claimed_snapshot = replace(
        snapshot,
        selected_pages=(2,),
        state=RecoveryState.CLAIMED,
        request_fingerprint="a" * 64,
        claim_id="claim-123",
        version=2,
    )
    claim = RecoveryClaim(
        claim_id="claim-123", request_fingerprint="a" * 64,
        snapshot=claimed_snapshot, acquired=True,
    )

    assert issue.token == raw_token
    assert raw_token not in repr(issue)
    assert "raw" not in repr(claim).lower()


def test_snapshot_and_claim_reject_internally_inconsistent_states() -> None:
    base = {
        "file_id": "file-123",
        "batch_id": "11111111-1111-4111-8111-111111111111",
        "source_result_version": 2,
        "page_count": 5,
        "suspected_pages": (2, 4),
        "selected_pages": None,
        "expires_at": NOW + timedelta(hours=1),
        "state": RecoveryState.ISSUED,
        "request_fingerprint": None,
        "claim_id": None,
        "corrected_input_version": None,
        "result_batch_id": None,
        "result_version": None,
        "error_code": None,
        "version": 1,
    }
    for changes in (
        {"state": RecoveryState.CLAIMED},
        {"state": RecoveryState.COMPLETED},
        {"state": RecoveryState.FAILED},
        {"state": RecoveryState.ISSUED, "claim_id": "claim-123"},
    ):
        values = dict(base)
        values.update(changes)
        with pytest.raises(ValueError):
            RecoverySnapshot(**values)

    claimed = RecoverySnapshot(
        **{
            **base,
            "selected_pages": (2,),
            "state": RecoveryState.CLAIMED,
            "request_fingerprint": "a" * 64,
            "claim_id": "claim-123",
        }
    )
    with pytest.raises(ValueError):
        RecoveryClaim(
            claim_id="claim-other",
            request_fingerprint="a" * 64,
            snapshot=claimed,
            acquired=True,
        )
