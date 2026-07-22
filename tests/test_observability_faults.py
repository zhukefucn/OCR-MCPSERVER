from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker
from ocr_mcp_server.services.orchestration import OrchestrationService
from ocr_mcp_server.settings import OrchestrationSettings


class RaisingSink:
    def __getattr__(self, name):
        if name.startswith(("observe_", "set_")):
            def raise_observation(*args, **kwargs):
                raise RuntimeError("private OCR text C:/customer/document.pdf")
            return raise_observation
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_raising_queue_observations_do_not_change_lifecycle_or_saturation():
    orchestration = OrchestrationService(
        object(), object(), OrchestrationSettings(wake_queue_capacity=1),
        worker_identity="worker", observability=RaisingSink(),
    )
    assert orchestration.notify_work() is True
    assert orchestration.notify_work() is False
    await orchestration.close()

    worker = SingleOwnerSecondaryOcrWorker(
        lambda: object(), queue_capacity=1, observability=RaisingSink()
    )
    await worker.close()
    assert worker.queue_depth == 0


def test_production_surface_contains_no_fault_injection_controls():
    root = Path(__file__).parents[1] / "src" / "ocr_mcp_server"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    forbidden = ("fault_injection", "inject_fault", "OCR_FAULT", "--fault")
    assert all(term not in text for term in forbidden)
