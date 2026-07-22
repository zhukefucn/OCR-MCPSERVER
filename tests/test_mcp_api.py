from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tomllib
from uuid import uuid4

import pytest

from ocr_mcp_server.api.contracts import (
    ArtifactReference,
    BatchStatusResponse,
    FileStatusResponse,
    OrientationReparseSubmission,
    ParseSubmission,
)
from ocr_mcp_server.api.gateway import GatewayFailure


class FakeGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.parse_request = None
        self.reparse_request = None
        self.status_id = None

    async def parse_documents(self, request, *, progress=None):
        self.parse_request = request
        if progress is not None:
            await progress(10, 100)
            await progress(10, 100)
            await progress(80, 100)
        if self.fail:
            raise RuntimeError("recognized private business text")
        return ParseSubmission(batch_id=str(uuid4()), status="queued")

    async def get_task_status(self, batch_id: str):
        self.status_id = batch_id
        if self.fail:
            raise GatewayFailure()
        return BatchStatusResponse(
            batch_id=batch_id,
            status="completed",
            progress=100,
            total_files=1,
            completed_files=1,
            failed_files=0,
            files=[
                FileStatusResponse(
                    file_id=str(uuid4()),
                    status="completed",
                    stage="completed",
                    progress=100,
                )
            ],
            artifacts=[
                ArtifactReference(
                    artifact_id=str(uuid4()),
                    download_url="https://api.example.test/v1/artifacts/result.zip",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            ],
        )

    async def reparse_with_page_orientation(self, request, *, progress=None):
        self.reparse_request = request
        if progress is not None:
            await progress(25, 100)
        return OrientationReparseSubmission(batch_id=str(uuid4()), status="queued")


class LeakyOutputGateway(FakeGateway):
    async def get_task_status(self, batch_id: str):
        return {
            "batch_id": batch_id,
            "status": "completed",
            "progress": 100,
            "total_files": 1,
            "completed_files": 1,
            "failed_files": 0,
            "files": [],
            "artifacts": [],
            "recognized_text": "SENSITIVE_OUTPUT_VALUE",
        }


def test_project_pins_fastmcp_2_without_heavy_ocr_dependencies() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    assert "fastmcp>=2.13.1,<3" in dependencies
    lowered = [item.lower() for item in dependencies]
    assert not any(
        item.startswith(("paddleocr", "paddlex", "mineru", "torch", "opencv"))
        for item in lowered
    )


def test_application_has_no_redis_import_or_runtime_configuration() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/ocr_mcp_server").rglob("*.py")
    ).lower()
    configuration = Path("config/example.yaml").read_text(encoding="utf-8").lower()
    assert "import redis" not in source
    assert "from redis" not in source
    assert "redis://" not in source
    assert "redis" not in configuration


@pytest.mark.asyncio
async def test_mcp_lists_exactly_three_curated_tools_with_safe_schemas() -> None:
    from fastmcp import Client
    from ocr_mcp_server.api.mcp import create_mcp_server

    server = create_mcp_server(FakeGateway())
    async with Client(server) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == {
        "parse_documents",
        "get_task_status",
        "reparse_with_page_orientation",
    }
    assert len(tools) == 3
    by_name = {tool.name: tool.inputSchema for tool in tools}
    assert by_name["parse_documents"]["properties"]["sources"]["minItems"] == 1
    assert by_name["parse_documents"]["properties"]["sources"]["maxItems"] == 20
    assert (
        by_name["reparse_with_page_orientation"]["properties"]["pages"][
            "anyOf"
        ][0]["maxItems"]
        == 500
    )
    serialized = str([tool.inputSchema for tool in tools])
    for forbidden in (
        "engine",
        "backend",
        "model",
        "device",
        "angle",
        "concurrency",
        "server_url",
        "path",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_mcp_calls_share_gateway_dtos_and_work_without_progress_token() -> None:
    from fastmcp import Client
    from ocr_mcp_server.api.mcp import create_mcp_server

    gateway = FakeGateway()
    file_id = str(uuid4())
    server = create_mcp_server(gateway)
    async with Client(server) as client:
        parse = await client.call_tool(
            "parse_documents",
            {"sources": [{"file_id": file_id}], "idempotency_key": "request-1"},
        )
        status = await client.call_tool(
            "get_task_status", {"batch_id": parse.structured_content["batch_id"]}
        )
        reparse = await client.call_tool(
            "reparse_with_page_orientation",
            {"recovery_token": "opaque-token_123", "pages": [1, 3]},
        )
    assert parse.structured_content["status"] == "queued"
    assert status.structured_content["status"] == "completed"
    assert reparse.structured_content["status"] == "queued"
    assert gateway.parse_request.sources[0].file_id == file_id
    assert gateway.reparse_request.pages == [1, 3]


@pytest.mark.asyncio
async def test_mcp_progress_is_monotonic_and_has_no_business_message() -> None:
    from fastmcp import Client
    from ocr_mcp_server.api.mcp import create_mcp_server

    observed: list[tuple[float, float | None, str | None]] = []

    async def progress_handler(
        progress: float, total: float | None, message: str | None
    ) -> None:
        observed.append((progress, total, message))

    async with Client(
        create_mcp_server(FakeGateway()), progress_handler=progress_handler
    ) as client:
        await client.call_tool(
            "parse_documents", {"sources": [{"file_id": str(uuid4())}]}
        )
    assert observed == [(10.0, 100.0, None), (80.0, 100.0, None)]


@pytest.mark.asyncio
async def test_mcp_errors_never_expose_causes_or_business_content() -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from ocr_mcp_server.api.mcp import create_mcp_server

    async with Client(create_mcp_server(FakeGateway(fail=True))) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "parse_documents", {"sources": [{"file_id": str(uuid4())}]}
            )
    serialized = str(exc_info.value)
    assert "recognized private business text" not in serialized
    assert serialized == "internal_error: The request could not be completed."


@pytest.mark.asyncio
async def test_mcp_validation_errors_mask_supplied_business_values() -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from ocr_mcp_server.api.mcp import create_mcp_server

    sensitive = "recognized-private-customer-name.pdf"
    async with Client(create_mcp_server(FakeGateway())) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "parse_documents",
                {
                    "sources": [{"url": f"http://files.example.test/{sensitive}"}],
                    "engine": sensitive,
                },
            )
    assert sensitive not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_protocol_masks_schema_validation_unknown_tool_and_output() -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from ocr_mcp_server.api.mcp import create_mcp_server

    cases = [
        (
            create_mcp_server(FakeGateway()),
            "parse_documents",
            {"sources": [{"file_id": "SENSITIVE_INPUT_VALUE"}]},
            "invalid_request: The request is invalid.",
            "SENSITIVE_INPUT_VALUE",
        ),
        (
            create_mcp_server(FakeGateway()),
            "unknown-SENSITIVE_TOOL_VALUE",
            {},
            "not_found: The requested tool was not found.",
            "SENSITIVE_TOOL_VALUE",
        ),
        (
            create_mcp_server(LeakyOutputGateway()),
            "get_task_status",
            {"batch_id": str(uuid4())},
            "internal_error: The request could not be completed.",
            "SENSITIVE_OUTPUT_VALUE",
        ),
    ]
    for server, tool_name, arguments, expected, sensitive in cases:
        async with Client(server) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool(tool_name, arguments)
        assert str(exc_info.value) == expected
        assert sensitive not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [True, 1.0, "1"])
async def test_mcp_reparse_rejects_non_integer_page_types_before_gateway(
    page: object,
) -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from ocr_mcp_server.api.mcp import create_mcp_server

    gateway = FakeGateway()
    async with Client(create_mcp_server(gateway)) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reparse_with_page_orientation",
                {"recovery_token": "opaque-token_123", "pages": [page]},
            )
    assert str(exc_info.value) == "invalid_request: The request is invalid."
    assert gateway.reparse_request is None


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_id", ["x" * 36, str(uuid4()).upper()])
async def test_mcp_status_rejects_noncanonical_ids_before_gateway(
    batch_id: str,
) -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from ocr_mcp_server.api.mcp import create_mcp_server

    gateway = FakeGateway()
    async with Client(create_mcp_server(gateway)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_task_status", {"batch_id": batch_id})
    assert gateway.status_id is None


def test_mcp_http_endpoint_is_fail_closed_before_protocol_code() -> None:
    from fastapi.testclient import TestClient
    from ocr_mcp_server.app import create_app
    from ocr_mcp_server.settings import AppSettings

    app = create_app(
        AppSettings(auth={"api_keys": ["a-secure-api-key-0000000000000001"]}),
        gateway=FakeGateway(),
    )
    with TestClient(app) as client:
        responses = [
            client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            ),
            client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Mcp-Session-Id": "opaque-session",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_task_status",
                        "arguments": {"batch_id": str(uuid4())},
                    },
                },
            ),
            client.delete(
                "/mcp", headers={"Mcp-Session-Id": "opaque-session"}
            ),
        ]
    assert [response.status_code for response in responses] == [401, 401, 401]
    assert all(
        response.json()["error"]["code"] == "authentication_failed"
        for response in responses
    )
