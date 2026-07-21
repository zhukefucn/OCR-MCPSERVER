from __future__ import annotations

from pathlib import Path

import pytest

from ocr_mcp_server.domain.errors import ConfigurationError
from ocr_mcp_server.domain.models import SecondaryOCREngine
from ocr_mcp_server.settings import AppSettings, load_settings


def test_settings_defaults_match_domain_constraints() -> None:
    settings = AppSettings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000
    assert settings.data_root == Path("data")
    assert settings.limits.max_files == 20
    assert settings.limits.max_file_size_bytes == 30 * 1024 * 1024
    assert settings.limits.max_pages == 500
    assert settings.limits.max_batch_size_bytes == 1024**3
    assert settings.retention.input_hours == 24
    assert settings.retention.intermediate_hours == 24
    assert settings.retention.result_hours == 24
    assert settings.retention.audit_metadata_days == 30
    assert settings.mineru.backend == "vlm-http-client"
    assert settings.secondary_ocr.engine is SecondaryOCREngine.PP_STRUCTURE_V3


def test_load_settings_reads_a_real_yaml_file(tmp_path: Path) -> None:
    config_file = tmp_path / "ocr.yaml"
    config_file.write_text(
        """
server:
  host: 0.0.0.0
  port: 9100
data_root: /srv/ocr-data
limits:
  max_files: 8
mineru:
  api_url: http://mineru-api:8000
  vlm_server_url: http://mineru-vlm:30000
secondary_ocr:
  engine: paddleocr_vl
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_file=config_file)

    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 9100
    assert settings.data_root == Path("/srv/ocr-data")
    assert settings.limits.max_files == 8
    assert str(settings.mineru.api_url) == "http://mineru-api:8000/"
    assert str(settings.mineru.vlm_server_url) == "http://mineru-vlm:30000/"
    assert settings.secondary_ocr.engine is SecondaryOCREngine.PADDLEOCR_VL


def test_environment_variables_override_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "ocr.yaml"
    config_file.write_text(
        "server:\n  port: 9100\nlimits:\n  max_files: 8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCR_SERVER__PORT", "9200")
    monkeypatch.setenv("OCR_LIMITS__MAX_FILES", "12")

    settings = load_settings(config_file=config_file)

    assert settings.server.port == 9200
    assert settings.limits.max_files == 12


def test_ocr_config_file_environment_variable_selects_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "selected.yaml"
    config_file.write_text("server:\n  port: 9300\n", encoding="utf-8")
    monkeypatch.setenv("OCR_CONFIG_FILE", str(config_file))

    settings = load_settings()

    assert settings.server.port == 9300


@pytest.mark.parametrize("engine", ["tesseract", "paddleocr-v4", ""])
def test_load_settings_rejects_an_unapproved_secondary_engine(
    tmp_path: Path, engine: str
) -> None:
    config_file = tmp_path / "invalid-engine.yaml"
    config_file.write_text(
        f"secondary_ocr:\n  engine: {engine!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(config_file=config_file)

    assert exc_info.value.code == "configuration_invalid"
    assert str(exc_info.value) == "Service configuration is invalid."


def test_load_settings_rejects_a_changed_mineru_backend_without_leaking_it(
    tmp_path: Path,
) -> None:
    sensitive_value = "recognized private business text"
    config_file = tmp_path / "invalid-backend.yaml"
    config_file.write_text(
        f"mineru:\n  backend: {sensitive_value!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(config_file=config_file)

    assert str(exc_info.value) == "Service configuration is invalid."
    assert sensitive_value not in str(exc_info.value)
