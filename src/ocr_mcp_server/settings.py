"""Validated deployment settings loaded from YAML and environment variables."""

from __future__ import annotations

import os
import ipaddress
from pathlib import Path
import re
from typing import Any, Literal

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .domain.constants import (
    DEFAULT_AUDIT_METADATA_RETENTION_DAYS,
    DEFAULT_INPUT_RETENTION_HOURS,
    DEFAULT_INTERMEDIATE_RETENTION_HOURS,
    DEFAULT_MAX_BATCH_SIZE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_FILE_SIZE_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_RESULT_RETENTION_HOURS,
)
from .domain.errors import ConfigurationError
from .domain.models import SecondaryOCREngine


class _SettingsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(_SettingsSection):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class AuthenticationSettings(_SettingsSection):
    """API keys remain secret in model representations and error paths."""

    api_keys: list[SecretStr] = Field(default_factory=list, max_length=32)

    @field_validator("api_keys", mode="before")
    @classmethod
    def validate_api_keys(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("API keys must be a list")
        canonical: list[str] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, str):
                raise ValueError("API keys must be strings")
            if (
                not 24 <= len(item) <= 256
                or item.strip() != item
                or any(ord(character) < 33 or ord(character) > 126 for character in item)
            ):
                raise ValueError("API key is malformed")
            if item in canonical:
                raise ValueError("API keys must be unique")
            canonical.append(item)
        return canonical


class LimitsSettings(_SettingsSection):
    max_files: int = Field(default=DEFAULT_MAX_FILES, ge=1)
    max_file_size_bytes: int = Field(default=DEFAULT_MAX_FILE_SIZE_BYTES, ge=1)
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1)
    max_batch_size_bytes: int = Field(default=DEFAULT_MAX_BATCH_SIZE_BYTES, ge=1)


class RemoteImportSettings(_SettingsSection):
    allowed_hosts: list[str] = Field(default_factory=list)
    max_redirects: int = Field(default=3, ge=0, le=10)
    timeout_seconds: float = Field(default=30, gt=0)
    max_image_pixels: int = Field(default=100_000_000, ge=1)

    @field_validator("allowed_hosts")
    @classmethod
    def canonicalize_allowed_hosts(cls, values: list[str]) -> list[str]:
        canonical: list[str] = []
        for value in values:
            candidate = value.strip().rstrip(".").lower()
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                pass
            else:
                raise ValueError("IP literals are not allowed")
            try:
                candidate = candidate.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise ValueError("invalid hostname") from exc
            if len(candidate) > 253 or not candidate:
                raise ValueError("invalid hostname")
            labels = candidate.split(".")
            if any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("invalid hostname")
            if candidate not in canonical:
                canonical.append(candidate)
        return canonical


class RetentionSettings(_SettingsSection):
    input_hours: int = Field(default=DEFAULT_INPUT_RETENTION_HOURS, ge=1)
    intermediate_hours: int = Field(
        default=DEFAULT_INTERMEDIATE_RETENTION_HOURS, ge=1
    )
    result_hours: int = Field(default=DEFAULT_RESULT_RETENTION_HOURS, ge=1)
    audit_metadata_days: int = Field(
        default=DEFAULT_AUDIT_METADATA_RETENTION_DAYS, ge=1
    )
    cleanup_batch_size: int = Field(default=25, ge=1, le=1000)
    cleanup_lease_seconds: int = Field(default=300, ge=1)
    cleanup_interval_seconds: int = Field(default=300, ge=1)

    @field_validator(
        "input_hours",
        "intermediate_hours",
        "result_hours",
        "audit_metadata_days",
        "cleanup_batch_size",
        "cleanup_lease_seconds",
        "cleanup_interval_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not retention settings")
        return value


class MinerUSettings(_SettingsSection):
    api_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8001")
    vlm_server_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:30000")
    backend: Literal["vlm-http-client"] = "vlm-http-client"
    connect_timeout_seconds: float = Field(default=10, gt=0)
    read_timeout_seconds: float = Field(default=60, gt=0)
    write_timeout_seconds: float = Field(default=60, gt=0)
    pool_timeout_seconds: float = Field(default=10, gt=0)
    task_deadline_seconds: float = Field(default=900, gt=0)
    poll_interval_seconds: float = Field(default=1, gt=0)
    retry_attempts: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)
    retry_max_backoff_seconds: float = Field(default=8, gt=0)
    max_compressed_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_uncompressed_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_archive_entries: int = Field(default=10_000, ge=1)

    @field_validator("api_url")
    @classmethod
    def require_safe_api_base(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("MinerU API URL must be an origin and path only")
        return value

    @model_validator(mode="after")
    def validate_retry_backoff(self) -> MinerUSettings:
        if self.retry_max_backoff_seconds < self.retry_backoff_seconds:
            raise ValueError("retry backoff cap must not be below its initial delay")
        return self


class SecondaryOCRSettings(_SettingsSection):
    engine: SecondaryOCREngine = SecondaryOCREngine.PP_STRUCTURE_V3
    device: Literal["cpu", "gpu"] = "cpu"
    queue_capacity: int = Field(default=8, ge=1, le=1024)
    classification_threshold: float = Field(default=0.8, gt=0, le=1)
    paddlex_config: Path | None = None
    formula_model_name: str = "PP-FormulaNet_plus-S"

    @field_validator("queue_capacity", "classification_threshold", mode="before")
    @classmethod
    def reject_boolean_numeric_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not numeric deployment settings")
        return value

    @field_validator("formula_model_name")
    @classmethod
    def require_formula_model_identity(cls, value: str) -> str:
        identity = value.strip()
        if not identity:
            raise ValueError("formula model identity is required")
        return identity


class StructuredContentSettings(_SettingsSection):
    max_characters: int = Field(default=1_000_000, ge=1, le=10_000_000)
    max_utf8_bytes: int = Field(default=4_000_000, ge=1, le=40_000_000)
    max_html_depth: int = Field(default=64, ge=1, le=256)
    max_html_elements: int = Field(default=20_000, ge=1, le=100_000)
    max_table_rows: int = Field(default=5_000, ge=1, le=20_000)
    max_table_cells: int = Field(default=10_000, ge=1, le=100_000)
    max_latex_repetition: int = Field(default=128, ge=1, le=1_024)
    max_artifact_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)

    @field_validator(
        "max_characters",
        "max_utf8_bytes",
        "max_html_depth",
        "max_html_elements",
        "max_table_rows",
        "max_table_cells",
        "max_latex_repetition",
        "max_artifact_bytes",
        mode="before",
    )
    @classmethod
    def reject_boolean_numeric_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not numeric deployment settings")
        return value

    @model_validator(mode="after")
    def validate_consistent_bounds(self) -> StructuredContentSettings:
        if (
            self.max_utf8_bytes < self.max_characters
            or self.max_artifact_bytes < self.max_utf8_bytes
            or self.max_html_depth > self.max_html_elements
            or self.max_table_rows > self.max_html_elements
            or self.max_table_cells > self.max_html_elements
        ):
            raise ValueError("structured-content limits are contradictory")
        return self

    def to_limits(self):
        from .services.structured_content import StructuredContentLimits

        return StructuredContentLimits(**self.model_dump())


class ArtifactSettings(_SettingsSection):
    max_artifact_bytes: int = Field(default=1024**3, ge=1)
    max_entry_bytes: int = Field(default=256 * 1024**2, ge=1)
    max_entry_count: int = Field(default=20_000, ge=1)
    max_markdown_bytes: int = Field(default=256 * 1024**2, ge=1)

    @field_validator(
        "max_artifact_bytes",
        "max_entry_bytes",
        "max_entry_count",
        "max_markdown_bytes",
        mode="before",
    )
    @classmethod
    def reject_boolean_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not artifact limits")
        return value

    def to_limits(self):
        from .services.artifacts import ArtifactLimits

        return ArtifactLimits(
            self.max_artifact_bytes,
            self.max_entry_bytes,
            self.max_entry_count,
            self.max_markdown_bytes,
        )


class DatabaseSettings(_SettingsSection):
    url: str = "sqlite+aiosqlite:///data/ocr.sqlite3"
    busy_timeout_ms: int = Field(default=5000, ge=1)

    @field_validator("url")
    @classmethod
    def require_async_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("unsupported database URL")
        return value


class OrchestrationSettings(_SettingsSection):
    worker_count: int = Field(default=1, ge=1, le=8)
    wake_queue_capacity: int = Field(default=64, ge=1, le=1024)
    lease_seconds: int = Field(default=120, ge=1)
    heartbeat_seconds: int = Field(default=30, ge=1)
    idle_poll_seconds: int = Field(default=1, ge=1)
    recovery_scan_seconds: int = Field(default=30, ge=1)
    notification_min_interval_seconds: int = Field(default=2, ge=1)

    @field_validator(
        "worker_count",
        "wake_queue_capacity",
        "lease_seconds",
        "heartbeat_seconds",
        "idle_poll_seconds",
        "recovery_scan_seconds",
        "notification_min_interval_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_numeric_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not numeric deployment settings")
        return value

    @model_validator(mode="after")
    def validate_heartbeat_lease_order(self) -> OrchestrationSettings:
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat must be shorter than the lease")
        return self


class AppSettings(BaseSettings):
    """Complete process-level configuration for the service."""

    model_config = SettingsConfigDict(
        env_prefix="OCR_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    auth: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    data_root: Path = Path("data")
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    remote_import: RemoteImportSettings = Field(default_factory=RemoteImportSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    mineru: MinerUSettings = Field(default_factory=MinerUSettings)
    secondary_ocr: SecondaryOCRSettings = Field(default_factory=SecondaryOCRSettings)
    structured_content: StructuredContentSettings = Field(default_factory=StructuredContentSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls
        return env_settings, init_settings, dotenv_settings, file_secret_settings


def load_settings(config_file: str | Path | None = None) -> AppSettings:
    """Load YAML settings, with ``OCR_`` environment values taking precedence."""

    selected_file = config_file or os.environ.get("OCR_CONFIG_FILE")
    yaml_values: dict[str, Any] = {}

    try:
        if selected_file is not None:
            raw_values = yaml.safe_load(Path(selected_file).read_text(encoding="utf-8"))
            if raw_values is not None and not isinstance(raw_values, dict):
                raise TypeError("the YAML document must be a mapping")
            yaml_values = raw_values or {}
        return AppSettings(**yaml_values)
    except (OSError, TypeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(cause=exc) from None
