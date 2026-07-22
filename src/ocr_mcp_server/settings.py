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
    ValidationError,
    field_validator,
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


class MinerUSettings(_SettingsSection):
    api_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8001")
    vlm_server_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:30000")
    backend: Literal["vlm-http-client"] = "vlm-http-client"


class SecondaryOCRSettings(_SettingsSection):
    engine: SecondaryOCREngine = SecondaryOCREngine.PP_STRUCTURE_V3


class DatabaseSettings(_SettingsSection):
    url: str = "sqlite+aiosqlite:///data/ocr.sqlite3"
    busy_timeout_ms: int = Field(default=5000, ge=1)

    @field_validator("url")
    @classmethod
    def require_async_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("unsupported database URL")
        return value


class AppSettings(BaseSettings):
    """Complete process-level configuration for the service."""

    model_config = SettingsConfigDict(
        env_prefix="OCR_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    data_root: Path = Path("data")
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    remote_import: RemoteImportSettings = Field(default_factory=RemoteImportSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    mineru: MinerUSettings = Field(default_factory=MinerUSettings)
    secondary_ocr: SecondaryOCRSettings = Field(default_factory=SecondaryOCRSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

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
