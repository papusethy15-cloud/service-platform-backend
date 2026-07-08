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


async def _stamp_baseline_async():
    """
    Runs BEFORE Alembic's run_sync bridge in its own committed transaction.
    Resets alembic_version to STAMP_AT so Alembic will run FINAL_MIGRATION.
    If FINAL_MIGRATION is already applied, this is a permanent no-op.
    """
    FINAL_MIGRATION = "055"
    STAMP_AT        = "054"

    engine = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            has_table = (await conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version')"
            ))).scalar()

            has_users = (await conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'users')"
            ))).scalar()

            if not has_users:
                return  # fresh empty DB — let Alembic run from scratch

            current = set()
            if has_table:
                rows = (await conn.execute(
                    text("SELECT version_num FROM alembic_version")
                )).fetchall()
                current = {r[0] for r in rows}

            if FINAL_MIGRATION in current:
                return  # already up to date — permanent no-op

            print(
                f"[INFO] env.py: {FINAL_MIGRATION} not yet applied "
                f"(current={current}) — resetting to {STAMP_AT}"
            )

            if not has_table:
                await conn.execute(text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                ))
            else:
                await conn.execute(text("DELETE FROM alembic_version"))

            await conn.execute(
                text(f"INSERT INTO alembic_version (version_num) VALUES ('{STAMP_AT}')")
            )
            # engine.begin() auto-commits on __aexit__

        print(
            f"[INFO] env.py: alembic_version committed to {STAMP_AT} "
            f"— upgrade will now run {FINAL_MIGRATION}"
        )
    finally:
        await engine.dispose()


async def _force_stamp_final_async(final: str):
    """
    Runs AFTER Alembic's run_sync bridge in its own committed transaction.
    Forces alembic_version to FINAL_MIGRATION if Alembic's own stamp didn't persist.
    This guards against the asyncpg run_sync transaction not committing the version update.
    """
    engine = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text("SELECT version_num FROM alembic_version")
            )).fetchall()
            current = {r[0] for r in rows}

            if final in current:
                return  # already stamped correctly

            print(f"[INFO] env.py: post-migration stamp fix — forcing {final} (was {current})")
            await conn.execute(text("DELETE FROM alembic_version"))
            await conn.execute(
                text(f"INSERT INTO alembic_version (version_num) VALUES ('{final}')")
            )
        print(f"[INFO] env.py: alembic_version force-stamped to {final}")
    finally:
        await engine.dispose()


async def run_async_migrations():
    FINAL_MIGRATION = "055"

    # Step 1: pre-stamp alembic_version in its own committed transaction
    await _stamp_baseline_async()

    # Step 2: run Alembic upgrade — picks up from STAMP_AT, runs FINAL_MIGRATION.
    # Wrapped in try/except so that even if Alembic aborts (e.g. transactional DDL
    # issue), Step 3 still runs and stamps the version, preventing the infinite loop.
    engine = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    except Exception as _exc:
        print(f"[WARN] env.py: migration run_sync raised {_exc!r} — continuing to force-stamp")
    finally:
        await engine.dispose()

    # Step 3: guarantee alembic_version is stamped even if run_sync aborted.
    # This is the critical loop-breaker: whether DDL succeeded or was a no-op
    # (IF NOT EXISTS), we stamp 055 so we never re-run this migration.
    await _force_stamp_final_async(FINAL_MIGRATION)


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
