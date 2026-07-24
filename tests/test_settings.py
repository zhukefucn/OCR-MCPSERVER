from __future__ import annotations

from pathlib import Path
import tomllib

import pytest
from pydantic import ValidationError

from ocr_mcp_server.domain.errors import ConfigurationError
from ocr_mcp_server.domain.models import SecondaryOCREngine
from ocr_mcp_server.settings import AppSettings, load_settings


def test_settings_defaults_match_domain_constraints() -> None:
    settings = AppSettings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000
    assert settings.data_root == Path("data")
    assert settings.limits.max_files == 20
    assert settings.limits.max_file_size_bytes == 60 * 1024 * 1024
    assert settings.limits.max_pages == 500
    assert settings.limits.max_batch_size_bytes == 1024**3
    assert settings.retention.input_hours == 24
    assert settings.retention.intermediate_hours == 24
    assert settings.retention.result_hours == 24
    assert settings.retention.audit_metadata_days == 30
    assert settings.mineru.backend == "vlm-http-client"
    assert settings.mineru.connect_timeout_seconds == 10
    assert settings.mineru.read_timeout_seconds == 60
    assert settings.mineru.write_timeout_seconds == 60
    assert settings.mineru.pool_timeout_seconds == 10
    assert settings.mineru.task_deadline_seconds == 900
    assert settings.mineru.poll_interval_seconds == 1
    assert settings.mineru.retry_attempts == 3
    assert settings.mineru.retry_backoff_seconds == 0.5
    assert settings.mineru.retry_max_backoff_seconds == 8
    assert settings.mineru.max_compressed_bytes == 512 * 1024 * 1024
    assert settings.mineru.max_uncompressed_bytes == 2 * 1024**3
    assert settings.mineru.max_archive_entries == 10_000
    assert settings.secondary_ocr.engine is SecondaryOCREngine.PP_STRUCTURE_V3
    assert settings.secondary_ocr.device == "cpu"
    assert settings.secondary_ocr.queue_capacity == 8
    assert settings.secondary_ocr.classification_threshold == 0.8
    assert settings.secondary_ocr.paddlex_config is None
    assert settings.secondary_ocr.formula_model_name == "PP-FormulaNet_plus-S"
    assert settings.secondary_ocr.orientation_model_dir is None
    assert (
        settings.secondary_ocr.orientation_model_name
        == "PP-LCNet_x1_0_doc_ori"
    )
    assert settings.secondary_ocr.orientation_assessment_timeout_seconds == 90
    assert settings.secondary_ocr.orientation_assessment_lease_seconds == 100
    assert settings.structured_content.max_utf8_bytes >= settings.structured_content.max_characters
    assert settings.structured_content.max_html_elements >= settings.structured_content.max_table_cells
    assert settings.structured_content.max_artifact_bytes >= settings.structured_content.max_utf8_bytes
    assert settings.database.url == "sqlite+aiosqlite:///data/ocr.sqlite3"
    assert settings.database.busy_timeout_ms == 5000
    assert settings.orchestration.worker_count == 1
    assert settings.orchestration.wake_queue_capacity == 64
    assert settings.orchestration.lease_seconds == 120
    assert settings.orchestration.heartbeat_seconds == 30
    assert settings.orchestration.idle_poll_seconds == 1
    assert settings.orchestration.recovery_scan_seconds == 30
    assert settings.orchestration.notification_min_interval_seconds == 2
    assert settings.remote_import.allowed_hosts == []
    assert settings.remote_import.max_redirects == 3
    assert settings.remote_import.timeout_seconds == 30
    assert settings.remote_import.max_image_pixels == 100_000_000
    assert settings.auth.api_keys == []


def test_authentication_keys_are_secret_and_reject_unsafe_values() -> None:
    secret = "a-secure-api-key-0000000000000001"
    settings = AppSettings(auth={"api_keys": [secret]})

    assert settings.auth.api_keys[0].get_secret_value() == secret
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())

    for keys in ([""], ["short"], [secret, secret], [True]):
        with pytest.raises(ValidationError) as exc_info:
            AppSettings(auth={"api_keys": keys})
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value.errors())
        assert secret not in exc_info.value.json()


def test_invalid_authentication_key_never_appears_in_validation_details() -> None:
    malformed = "recognized-private-authentication-key"
    for keys in ([malformed + "\n"], [malformed, malformed]):
        with pytest.raises(ValidationError) as exc_info:
            AppSettings(auth={"api_keys": keys})
        assert malformed not in str(exc_info.value)
        assert malformed not in repr(exc_info.value.errors())
        assert malformed not in exc_info.value.json()


@pytest.mark.parametrize(
    "field",
    [
        "max_characters",
        "max_utf8_bytes",
        "max_html_depth",
        "max_html_elements",
        "max_table_rows",
        "max_table_cells",
        "max_latex_repetition",
        "max_artifact_bytes",
    ],
)
def test_structured_content_settings_reject_boolean_numbers(field: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(structured_content={field: True})


def test_structured_content_settings_reject_contradictory_limits() -> None:
    with pytest.raises(ValidationError):
        AppSettings(structured_content={"max_characters": 1000, "max_utf8_bytes": 999})


def test_structured_content_settings_reject_values_above_hard_caps() -> None:
    with pytest.raises(ValidationError):
        AppSettings(structured_content={"max_html_depth": 257})


def test_remote_import_hosts_are_canonicalized_and_deduplicated() -> None:
    settings = AppSettings(
        remote_import={"allowed_hosts": ["FILES.Example.COM.", "files.example.com"]}
    )

    assert settings.remote_import.allowed_hosts == ["files.example.com"]


@pytest.mark.parametrize(
    "host", ["*.example.com", "127.0.0.1", "::1", "bad host", ""]
)
def test_remote_import_rejects_wildcards_ip_literals_and_invalid_hosts(
    host: str,
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(remote_import={"allowed_hosts": [host]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_redirects", -1),
        ("max_redirects", 11),
        ("timeout_seconds", 0),
        ("max_image_pixels", 0),
    ],
)
def test_remote_import_rejects_out_of_range_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AppSettings(remote_import={field: value})


def test_project_metadata_declares_file_intake_dependencies() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime = metadata["project"]["dependencies"]
    development = metadata["project"]["optional-dependencies"]["dev"]

    assert "httpx>=0.28,<0.29" in runtime
    assert "Pillow>=11,<12" in runtime
    assert "pypdf>=5,<6" in runtime
    assert "respx>=0.22,<0.23" in development


def test_example_yaml_documents_remote_import_defaults() -> None:
    settings = load_settings(config_file=Path("config/example.yaml"))

    assert settings.remote_import.allowed_hosts == []
    assert settings.remote_import.max_redirects == 3
    assert settings.remote_import.timeout_seconds == 30
    assert settings.remote_import.max_image_pixels == 100_000_000


@pytest.mark.parametrize(
    "url",
    ["sqlite:///data/ocr.sqlite3", "postgresql+asyncpg://host/db", "file.db"],
)
def test_settings_reject_non_aiosqlite_database_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(database={"url": url})


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_timeout_seconds", 0),
        ("read_timeout_seconds", 0),
        ("write_timeout_seconds", 0),
        ("pool_timeout_seconds", 0),
        ("task_deadline_seconds", 0),
        ("poll_interval_seconds", 0),
        ("retry_attempts", -1),
        ("retry_attempts", 11),
        ("retry_backoff_seconds", 0),
        ("retry_max_backoff_seconds", 0),
        ("max_compressed_bytes", 0),
        ("max_uncompressed_bytes", 0),
        ("max_archive_entries", 0),
    ],
)
def test_mineru_rejects_invalid_timeout_retry_and_archive_limits(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(mineru={field: value})


def test_mineru_rejects_retry_backoff_cap_below_initial_delay() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            mineru={
                "retry_backoff_seconds": 2,
                "retry_max_backoff_seconds": 1,
            }
        )


def test_example_yaml_documents_mineru_adapter_defaults() -> None:
    mineru = load_settings(config_file=Path("config/example.yaml")).mineru

    assert mineru.backend == "vlm-http-client"
    assert mineru.connect_timeout_seconds == 10
    assert mineru.read_timeout_seconds == 60
    assert mineru.write_timeout_seconds == 60
    assert mineru.pool_timeout_seconds == 10
    assert mineru.task_deadline_seconds == 900
    assert mineru.poll_interval_seconds == 1
    assert mineru.retry_attempts == 3
    assert mineru.retry_backoff_seconds == 0.5
    assert mineru.retry_max_backoff_seconds == 8
    assert mineru.max_compressed_bytes == 512 * 1024 * 1024
    assert mineru.max_uncompressed_bytes == 2 * 1024**3
    assert mineru.max_archive_entries == 10_000


def test_secondary_ocr_settings_load_from_yaml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "ocr.yaml"
    config_file.write_text(
        "secondary_ocr:\n"
        "  device: cpu\n"
        "  queue_capacity: 3\n"
        "  classification_threshold: 0.7\n"
        "  paddlex_config: /models/pipeline.yaml\n"
        "  formula_model_name: PP-FormulaNet_plus-M\n"
        "  orientation_model_dir: /models/doc-orientation\n"
        "  orientation_model_name: Trusted-Doc-Ori\n"
        "  orientation_assessment_timeout_seconds: 40\n"
        "  orientation_assessment_lease_seconds: 50\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCR_SECONDARY_OCR__DEVICE", "gpu")
    monkeypatch.setenv("OCR_SECONDARY_OCR__QUEUE_CAPACITY", "4")

    settings = load_settings(config_file=config_file).secondary_ocr

    assert settings.device == "gpu"
    assert settings.queue_capacity == 4
    assert settings.classification_threshold == 0.7
    assert settings.paddlex_config == Path("/models/pipeline.yaml")
    assert settings.formula_model_name == "PP-FormulaNet_plus-M"
    assert settings.orientation_model_dir == Path("/models/doc-orientation")
    assert settings.orientation_model_name == "Trusted-Doc-Ori"
    assert settings.orientation_assessment_timeout_seconds == 40
    assert settings.orientation_assessment_lease_seconds == 50


def test_secondary_ocr_settings_accept_explicit_first_gpu() -> None:
    settings = AppSettings(
        secondary_ocr={"device": "gpu:0"}
    ).secondary_ocr

    assert settings.device == "gpu:0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device", "cuda"),
        ("queue_capacity", 0),
        ("queue_capacity", True),
        ("classification_threshold", 0),
        ("classification_threshold", True),
        ("classification_threshold", 1.01),
        ("classification_threshold", float("nan")),
        ("formula_model_name", ""),
        ("orientation_model_name", ""),
        ("orientation_assessment_timeout_seconds", 0),
        ("orientation_assessment_lease_seconds", 0),
        ("unexpected_runtime_option", True),
    ],
)
def test_secondary_ocr_settings_reject_invalid_or_unknown_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(secondary_ocr={field: value})


def test_example_yaml_documents_secondary_ocr_deployment_defaults() -> None:
    settings = load_settings(config_file=Path("config/example.yaml")).secondary_ocr

    assert settings.engine is SecondaryOCREngine.PP_STRUCTURE_V3
    assert settings.device == "cpu"
    assert settings.queue_capacity == 8
    assert settings.classification_threshold == 0.8
    assert settings.paddlex_config is None
    assert settings.formula_model_name == "PP-FormulaNet_plus-S"
    assert settings.orientation_model_dir is None
    assert settings.orientation_model_name == "PP-LCNet_x1_0_doc_ori"
    assert settings.orientation_assessment_timeout_seconds == 90
    assert settings.orientation_assessment_lease_seconds == 100


def test_orientation_assessment_timeout_lease_and_task_lease_are_ordered() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            secondary_ocr={
                "orientation_assessment_timeout_seconds": 10,
                "orientation_assessment_lease_seconds": 10,
            }
        )
    with pytest.raises(ValidationError):
        AppSettings(
            secondary_ocr={
                "orientation_assessment_timeout_seconds": 10,
                "orientation_assessment_lease_seconds": 30,
            },
            orchestration={"lease_seconds": 30, "heartbeat_seconds": 5},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_count", 0),
        ("worker_count", 9),
        ("worker_count", True),
        ("wake_queue_capacity", 0),
        ("wake_queue_capacity", 1025),
        ("wake_queue_capacity", True),
        ("lease_seconds", 0),
        ("heartbeat_seconds", 0),
        ("idle_poll_seconds", 0),
        ("recovery_scan_seconds", 0),
        ("notification_min_interval_seconds", 0),
    ],
)
def test_orchestration_settings_reject_invalid_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(orchestration={field: value})


def test_orchestration_heartbeat_must_be_strictly_shorter_than_lease() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            orchestration={"lease_seconds": 30, "heartbeat_seconds": 30}
        )


def test_example_yaml_documents_orchestration_defaults() -> None:
    orchestration = load_settings(config_file=Path("config/example.yaml")).orchestration

    assert orchestration.worker_count == 1
    assert orchestration.wake_queue_capacity == 64
    assert orchestration.lease_seconds == 120
    assert orchestration.heartbeat_seconds == 30
    assert orchestration.idle_poll_seconds == 1
    assert orchestration.recovery_scan_seconds == 30
    assert orchestration.notification_min_interval_seconds == 2


@pytest.mark.parametrize(
    "value",
    [0, -1, True, float("nan"), float("inf"), float("-inf")],
)
def test_health_probe_timeout_rejects_non_positive_non_finite_and_boolean_values(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(health={"probe_timeout_seconds": value})


def test_health_settings_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppSettings(health={"probe_timeout_seconds": 1, "fault_injection": True})


def test_example_yaml_documents_health_probe_timeout() -> None:
    health = load_settings(config_file=Path("config/example.yaml")).health
    assert health.probe_timeout_seconds == 3.0


def test_health_probe_timeout_accepts_numeric_environment_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCR_HEALTH__PROBE_TIMEOUT_SECONDS", "1.5")
    assert AppSettings().health.probe_timeout_seconds == 1.5


def test_example_yaml_documents_structured_merge_safety_limits() -> None:
    structured = load_settings(config_file=Path("config/example.yaml")).structured_content
    assert structured.max_characters > 0
    assert structured.max_html_elements >= structured.max_table_cells
    assert structured.max_artifact_bytes >= structured.max_utf8_bytes


def test_project_metadata_has_no_local_mineru_or_torch_dependency() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = [
        item.lower()
        for group in (
            metadata["project"]["dependencies"],
            metadata["project"]["optional-dependencies"]["dev"],
        )
        for item in group
    ]

    assert not any(
        item.startswith(("mineru", "torch", "transformers", "vllm"))
        for item in dependencies
    )


@pytest.mark.parametrize(
    "api_url",
    [
        "https://user:password@api.example.test",
        "https://api.example.test/tasks?secret=value",
        "https://api.example.test/tasks#secret",
    ],
)
def test_mineru_api_base_rejects_credentials_query_and_fragment(api_url: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(mineru={"api_url": api_url})
