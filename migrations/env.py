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
    `alembic upgrade head` will correctly apply all pending migrations.

    IMPORTANT: Do NOT call connection.commit() here.
    This function runs inside `connection.run_sync(do_run_migrations)` which
    is inside an asyncpg-managed async connection. Calling .commit() manually
    ends the implicit transaction prematurely and can cause the subsequent
    context.begin_transaction() / context.run_migrations() to run in an
    auto-committed state where DDL is visible but alembic_version updates
    are lost — exactly the bug that caused 047/048/049/050 to be perpetually
    skipped on the VPS.

    All writes here are part of the same transaction that Alembic commits
    via context.begin_transaction() in do_run_migrations().
    """
    from sqlalchemy import text

    # FINAL_MIGRATION: once this revision is recorded, this function is a no-op forever.
    # Update this to the latest migration revision whenever a new "fix chain" is added.
    FINAL_MIGRATION = '053'
    STAMP_AT        = '052'  # one step before FINAL_MIGRATION

    has_version_table = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'alembic_version')"
    )).scalar()

    current_versions = set()
    if has_version_table:
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        current_versions = {r[0] for r in rows}

    # Already done — permanent no-op
    if FINAL_MIGRATION in current_versions:
        return

    has_users = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'users')"
    )).scalar()

    # Fresh empty DB — let Alembic run everything from scratch normally
    if not has_users:
        return

    # DB has real schema but FINAL_MIGRATION has not been applied.
    # Replace whatever is in alembic_version with STAMP_AT so upgrade()
    # will run exactly FINAL_MIGRATION and nothing else.
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
    # NOTE: No connection.commit() here — Alembic's context.begin_transaction()
    # in do_run_migrations() owns and commits this transaction.
    print(f"[INFO] env.py: alembic_version reset to {STAMP_AT} — upgrade will now run {FINAL_MIGRATION}")


def do_run_migrations(connection):
    _maybe_stamp_baseline(connection)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
