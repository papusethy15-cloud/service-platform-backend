"""Add CANCELLED to paymentstatus enum

Revision ID: 044
Revises: 043
Create Date: 2026-07-06

Root cause of "Payment failed" bug:
  When admin/CCO/technician collects CASH on an invoice that has a PAY_LATER
  PENDING transaction, the backend tries to auto-void the stale PAY_LATER by
  setting status = CANCELLED. But the paymentstatus PostgreSQL enum never had
  CANCELLED, so the DB rejects the UPDATE with:
    ERROR: invalid input value for enum paymentstatus: "CANCELLED"
  SQLAlchemy rolls back the entire transaction -> frontend shows "Payment failed".
"""
from alembic import op
from sqlalchemy import text

# Alembic chain identifiers
revision = '044'
down_revision = '043'
branch_labels = None
depends_on = None

# IMPORTANT: ALTER TYPE ... ADD VALUE cannot run inside a transaction block in
# PostgreSQL. Setting transaction = False tells Alembic to run this migration
# in autocommit mode (no BEGIN/COMMIT wrapper) which is required for enum
# value additions.
def upgrade() -> None:
    # ALTER TYPE ADD VALUE cannot run inside a transaction block.
    # We use raw COMMIT/BEGIN to step outside Alembic's transaction temporarily.
    bind = op.get_bind()
    result = bind.execute(text(
        "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'paymentstatus' AND e.enumlabel = 'CANCELLED'"
    ))
    if not result.fetchone():
        bind.execute(text("COMMIT"))
        bind.execute(text("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'CANCELLED'"))
        bind.execute(text("BEGIN"))


def downgrade() -> None:
    # Postgres does not support removing enum values without recreating the type.
    pass
