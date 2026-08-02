from logging.config import fileConfig
import asyncio
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context
from app.core.config import settings
from app.models.base import BaseModel
import app.models  # noqa: F401

config = context.config

_DB_URL = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = BaseModel.metadata


def run_migrations_offline():
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _ensure_baseline_async():
    """
    One-time baseline stamp for databases that were created before Alembic
    was introduced. Checks if the DB has data (users table exists) but no
    alembic_version row, and stamps it at the last known-good revision so
    Alembic doesn't try to re-run every migration from scratch.

    Safe to call on every upgrade — it's a permanent no-op once the
    alembic_version table has any row.
    """
    # The revision BEFORE the first migration that was NOT yet applied
    # to the legacy VPS DB when Alembic was first introduced.
    BASELINE_STAMP = "080"  # updated: stamp at current head so legacy DBs skip all migrations

    engine = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            has_users = (await conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'users')"
            ))).scalar()

            if not has_users:
                return  # fresh empty DB — Alembic runs all migrations normally

            has_alembic = (await conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version')"
            ))).scalar()

            if has_alembic:
                row_count = (await conn.execute(
                    text("SELECT COUNT(*) FROM alembic_version")
                )).scalar()
                if row_count and row_count > 0:
                    return  # already has a version stamp — nothing to do

            # DB has data but no version row — stamp the baseline
            print(f"[INFO] env.py: legacy DB detected — stamping baseline {BASELINE_STAMP}")
            if not has_alembic:
                await conn.execute(text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                ))
            else:
                await conn.execute(text("DELETE FROM alembic_version"))

            await conn.execute(
                text(f"INSERT INTO alembic_version (version_num) VALUES ('{BASELINE_STAMP}')")
            )
            print(f"[INFO] env.py: baseline stamp {BASELINE_STAMP} committed")
    finally:
        await engine.dispose()


async def run_async_migrations():
    # Step 1: one-time baseline stamp (no-op if alembic_version already has a row)
    await _ensure_baseline_async()

    # Step 2: run Alembic migrations normally — errors propagate so you see them
    engine = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
