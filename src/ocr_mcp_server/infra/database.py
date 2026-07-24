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
