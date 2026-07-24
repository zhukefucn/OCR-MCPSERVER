"""Async SQLite engine, session factory, pragmas, and schema setup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..domain.constants import DEFAULT_MAX_FILE_SIZE_BYTES
from .task_models import Base

SessionFactory = async_sessionmaker[AsyncSession]


def create_database_engine(url: str, *, busy_timeout_ms: int = 5000) -> AsyncEngine:
    if not url.startswith("sqlite+aiosqlite:///") or busy_timeout_ms < 1:
        raise ValueError("invalid SQLite database configuration")
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def initialize_schema(engine: AsyncEngine) -> None:
    database = engine.url.database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_migrate_batch_source_fingerprint)
        await connection.run_sync(_migrate_orientation_takeover_columns)
        await connection.run_sync(_migrate_orientation_assessment_claim_columns)
        await connection.run_sync(_migrate_orientation_recovery_file_limit)


def _migrate_batch_source_fingerprint(connection) -> None:
    """Add the content-free parse fingerprint to pre-Task-13 databases."""

    existing = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(batches)")
    }
    if "source_fingerprint" not in existing:
        connection.exec_driver_sql(
            "ALTER TABLE batches ADD COLUMN source_fingerprint VARCHAR(64)"
        )


def _migrate_orientation_takeover_columns(connection) -> None:
    """Apply the additive Task 11 takeover-proof schema migration."""
    existing = {
        row[1]
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(orientation_recoveries)"
        )
    }
    additions = {
        "accepted_input_file_id": "VARCHAR(36)",
        "accepted_input_sha256": "VARCHAR(64)",
        "accepted_input_size_bytes": "INTEGER",
        "corrected_input_file_id": "VARCHAR(36)",
        "corrected_input_sha256": "VARCHAR(64)",
        "corrected_input_size_bytes": "INTEGER",
        "expected_corrected_input_version": "INTEGER",
        "terminal_observed": "BOOLEAN NOT NULL DEFAULT 0",
    }
    for name, column_type in additions.items():
        if name not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE orientation_recoveries ADD COLUMN {name} {column_type}"
            )


def _migrate_orientation_assessment_claim_columns(connection) -> None:
    """Add bounded claim ownership to databases created before Task 13E."""

    existing = {
        row[1]
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(orientation_assessments)"
        )
    }
    additions = {
        "claim_token": "VARCHAR(64)",
        "lease_expires_at": "DATETIME",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, column_type in additions.items():
        if name not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE orientation_assessments "
                f"ADD COLUMN {name} {column_type}"
            )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "ux_orientation_assessment_claim_token "
        "ON orientation_assessments(claim_token) "
        "WHERE claim_token IS NOT NULL"
    )


def _migrate_orientation_recovery_file_limit(connection) -> None:
    """Expand legacy 30MiB recovery CHECK constraints without losing rows."""

    table_name = "orientation_recoveries"
    temporary_name = "orientation_recoveries__file_limit_migration"
    legacy_limit = str(30 * 1024 * 1024)
    current_limit = str(DEFAULT_MAX_FILE_SIZE_BYTES)
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).one_or_none()
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("orientation recovery schema unavailable")
    create_sql = row[0]
    if legacy_limit not in create_sql:
        return
    if (
        create_sql.count(legacy_limit) != 2
        or current_limit in create_sql
        or connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (temporary_name,),
        ).one_or_none()
        is not None
    ):
        raise RuntimeError("orientation recovery schema migration rejected")
    create_prefix = f"CREATE TABLE {table_name}"
    if not create_sql.startswith(create_prefix):
        raise RuntimeError("orientation recovery schema migration rejected")
    columns = [
        row[1]
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})")
    ]
    if not columns or any(not isinstance(column, str) or not column for column in columns):
        raise RuntimeError("orientation recovery schema migration rejected")
    quoted_columns = ", ".join(
        f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
    )
    migrated_sql = create_sql.replace(
        create_prefix, f"CREATE TABLE {temporary_name}", 1
    ).replace(legacy_limit, current_limit)
    connection.exec_driver_sql(migrated_sql)
    connection.exec_driver_sql(
        f"INSERT INTO {temporary_name} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {table_name}"
    )
    connection.exec_driver_sql(f"DROP TABLE {table_name}")
    connection.exec_driver_sql(
        f"ALTER TABLE {temporary_name} RENAME TO {table_name}"
    )
