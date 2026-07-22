from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi.testclient import TestClient

from ocr_mcp_server.app import create_app
from ocr_mcp_server.settings import AppSettings


@dataclass
class CountingGateway:
    calls: int = 0

    async def get_task_status(self, batch_id: str):
        self.calls += 1
        raise AssertionError("authentication must run before gateway code")


def _client(keys: list[str], gateway: object) -> TestClient:
    return TestClient(create_app(AppSettings(auth={"api_keys": keys}), gateway=gateway))


def test_liveness_is_public_and_contains_no_configuration_detail() -> None:
    with _client([], CountingGateway()) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_api_fails_closed_when_no_key_is_configured() -> None:
    gateway = CountingGateway()
    with _client([], gateway) as client:
        response = client.get(f"/v1/tasks/{uuid4()}")
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "authentication_unavailable",
            "message": "Authentication is not configured.",
        }
    }
    assert gateway.calls == 0


def test_bearer_and_api_key_credentials_are_accepted() -> None:
    class Gateway:
        calls = 0

        async def get_task_status(self, batch_id: str):
            self.calls += 1
            from ocr_mcp_server.api.contracts import BatchStatusResponse

            return BatchStatusResponse(
                batch_id=batch_id,
                status="completed",
                progress=100,
                total_files=1,
                completed_files=1,
                failed_files=0,
                files=[],
                artifacts=[],
            )

    gateway = Gateway()
    with _client(["a-secure-api-key-0000000000000001"], gateway) as client:
        bearer = client.get(
            f"/v1/tasks/{uuid4()}",
            headers={"Authorization": "Bearer a-secure-api-key-0000000000000001"},
        )
        header = client.get(
            f"/v1/tasks/{uuid4()}",
            headers={"X-API-Key": "a-secure-api-key-0000000000000001"},
        )
    assert bearer.status_code == 200
    assert header.status_code == 200
    assert gateway.calls == 2


def test_invalid_malformed_and_conflicting_credentials_are_rejected_safely() -> None:
    secret = "a-secure-api-key-0000000000000001"
    gateway = CountingGateway()
    with _client([secret], gateway) as client:
        responses = [
            client.get(f"/v1/tasks/{uuid4()}"),
            client.get(
                f"/v1/tasks/{uuid4()}", headers={"Authorization": "Basic opaque"}
            ),
            client.get(
                f"/v1/tasks/{uuid4()}", headers={"X-API-Key": "wrong-key-value"}
            ),
            client.get(
                f"/v1/tasks/{uuid4()}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "X-API-Key": "different-secure-key-00000000000002",
                },
            ),
        ]
    assert [response.status_code for response in responses] == [401, 401, 401, 401]
    for response in responses:
        serialized = response.text
        assert response.json()["error"]["code"] == "authentication_failed"
        assert secret not in serialized
        assert "different-secure" not in serialized
    assert gateway.calls == 0
