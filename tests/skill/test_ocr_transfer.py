from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "skills" / "using-ocr-mcpserver" / "scripts" / "ocr-transfer.ps1"


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
