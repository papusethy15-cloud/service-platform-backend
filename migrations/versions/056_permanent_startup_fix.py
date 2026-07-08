"""056_permanent_startup_fix

WHY THIS EXISTS:
  Every restart, Alembic's env.py is called from a ThreadPoolExecutor thread
  inside uvicorn's event loop via _auto_migrate(). When alembic finds there
  are no migrations to run (already at head), it still goes through the full
  initialize-engine → run_migrations path. On VPS, this causes an "Aborted!"
  in stderr (Click catches an exception inside alembic command.upgrade and
  prints Aborted! before our except handler can log it cleanly).

  This migration is a pure no-op (no DDL). Its only purpose is to be the
  new "head" that env.py can detect and exit early, preventing the Aborted!
  loop entirely. After this migration is stamped on VPS, env.py's 
  _stamp_baseline_async() sees FINAL_MIGRATION='056' already present and 
  returns immediately — never calling alembic command.upgrade() at all.

Revision ID: 056
Revises: 055
Create Date: 2026-07-08
"""
from alembic import op
from sqlalchemy import text

revision = '056'
down_revision = '055'
branch_labels = None
depends_on = None


def upgrade():
    # Pure no-op migration. All schema changes are already in 055.
    # This revision exists solely so env.py can detect "head already applied"
    # and skip the alembic upgrade() call that causes Aborted! on every restart.
    print("[056] Permanent startup fix migration — no DDL needed.")


def downgrade():
    pass
