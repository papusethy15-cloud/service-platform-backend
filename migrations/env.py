from logging.config import fileConfig
import asyncio
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine
from alembic import context
from app.core.config import settings
from app.models.base import BaseModel
import app.models  # noqa: F401

config = context.config

# NOTE: We intentionally do NOT use config.set_main_option() to set the DB URL.
# The password contains %-encoded characters (e.g. %40, %23) which Python's
# configparser misinterprets as interpolation syntax and raises ValueError.
# Instead we build the URL directly and pass it to the engine, bypassing
# configparser entirely.
_DB_URL = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = BaseModel.metadata

def run_migrations_offline():
    # Pass URL directly — never through config.get_main_option()
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations():
    # Build the engine directly from the URL string, not from configparser section
    connectable = create_async_engine(_DB_URL, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def _maybe_stamp_baseline(connection):
    """
    Ensures the VPS alembic_version table is in a state where
    `alembic upgrade head` will correctly apply migrations 047, 048, and 049.

    The VPS DB was bootstrapped before proper Alembic tracking was in place.
    Over several fix attempts the alembic_version table may contain various
    combinations of revision IDs — none of which include '049'.

    Strategy: if the DB already has the real schema (users table exists) AND
    '049' is not yet recorded as applied, clear whatever is in alembic_version
    and stamp at '046'. This guarantees upgrade() will run 047, 048, and 049
    and nothing else, regardless of what legacy IDs are currently present.
    """
    from sqlalchemy import text

    # FINAL_MIGRATION is the last migration in the hotfix chain.
    # Once it is stamped, this function becomes a permanent no-op.
    FINAL_MIGRATION = '049'
    STAMP_AT        = '046'  # one step before 047 (the first hotfix)

    has_version_table = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'alembic_version')"
    )).scalar()

    current_versions = set()
    if has_version_table:
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        current_versions = {r[0] for r in rows}

    # Already done — nothing to fix
    if FINAL_MIGRATION in current_versions:
        return

    has_users = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'users')"
    )).scalar()

    # Fresh empty DB — let Alembic run everything from scratch normally
    if not has_users:
        return

    # DB has real schema but 049 has not been applied yet.
    # Whatever is currently in alembic_version, replace it with STAMP_AT
    # so upgrade() will run 047, 048, and 049.
    print(f"[INFO] env.py: {FINAL_MIGRATION} not yet applied (current={current_versions}) — resetting to {STAMP_AT}")

    if not has_version_table:
        connection.execute(text(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
    else:
        connection.execute(text("DELETE FROM alembic_version"))

    connection.execute(text(f"INSERT INTO alembic_version (version_num) VALUES ('{STAMP_AT}')"))
    connection.commit()
    print(f"[INFO] env.py: alembic_version reset to {STAMP_AT} — upgrade will now run 047+048+049")


def do_run_migrations(connection):
    _maybe_stamp_baseline(connection)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
