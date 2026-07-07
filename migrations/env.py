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
    Handles two legacy VPS states:

    State A — no alembic_version table at all (DB pre-dates Alembic):
        Stamp at 046 so upgrade() only runs 047+.

    State B — alembic_version has only the original auto-generated revisions
        (91baaab49547 / fc36bebf9204) but NOT the numbered chain (001→047).
        This happens when the app was first deployed with just those two
        auto-generated migrations, then the numbered chain was added later
        but the VPS was never re-migrated.  Alembic sees 91baaab49547 and
        fc36bebf9204 as the current heads and considers everything up to date,
        so upgrade() runs ZERO migrations even though 001-047 were never applied.
        Fix: replace the old head entries with 046 so upgrade() runs only 047+.
    """
    from sqlalchemy import text

    has_version_table = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version')"
    )).scalar()

    current_versions = set()
    if has_version_table:
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        current_versions = {r[0] for r in rows}

    has_users = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users')"
    )).scalar()

    # The OLD auto-generated revision IDs that predate the numbered chain
    OLD_AUTO_REVISIONS = {'91baaab49547', 'fc36bebf9204'}
    # The numbered chain head — if this is present, the chain has been applied
    NUMBERED_HEAD = '047'

    if not has_users:
        return  # Fresh empty DB — let Alembic run everything normally

    if not has_version_table or not current_versions:
        # State A: schema exists but zero Alembic tracking
        print("[INFO] env.py: DB has schema but no alembic_version — stamping baseline at 046")
        if not has_version_table:
            connection.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            ))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('046')"))
        connection.commit()
        print("[INFO] env.py: stamped at 046")
        return

    if current_versions == OLD_AUTO_REVISIONS or current_versions.issubset(OLD_AUTO_REVISIONS):
        # State B: only the old auto-generated revisions are present, numbered chain missing
        print(f"[INFO] env.py: found legacy revision IDs {current_versions} — replacing with numbered chain baseline")
        for old_rev in current_versions:
            connection.execute(text(f"DELETE FROM alembic_version WHERE version_num = '{old_rev}'"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('046')"))
        connection.commit()
        print("[INFO] env.py: replaced legacy IDs with 046, upgrade will now apply only new migrations (047+)")


def do_run_migrations(connection):
    _maybe_stamp_baseline(connection)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
