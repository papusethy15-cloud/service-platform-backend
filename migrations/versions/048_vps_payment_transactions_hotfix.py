"""048_vps_payment_transactions_hotfix

VPS hotfix: adds columns to payment_transactions that were defined in
migration 036_pay_later_proper but never applied on the VPS because the
VPS DB was bootstrapped from a pre-036 snapshot.

Columns added (both idempotent with IF NOT EXISTS):
  - payment_transactions.due_collect_at   TIMESTAMP WITH TIME ZONE
  - payment_transactions.last_reminder_at TIMESTAMP WITH TIME ZONE

Also ensures the PAY_LATER enum value exists on paymentmethod.

NOTE: ALTER TYPE ADD VALUE cannot run inside a PostgreSQL transaction block.
      We use AUTOCOMMIT isolation for that single statement only.

Revision ID: 048
Revises: 047
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '048'
down_revision = '047'
branch_labels = None
depends_on = None


def upgrade():
    # ── payment_transactions: due_collect_at ─────────────────────────────
    op.execute(
        "ALTER TABLE payment_transactions "
        "ADD COLUMN IF NOT EXISTS due_collect_at TIMESTAMP WITH TIME ZONE"
    )

    # ── payment_transactions: last_reminder_at ───────────────────────────
    op.execute(
        "ALTER TABLE payment_transactions "
        "ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP WITH TIME ZONE"
    )

    # ── paymentmethod enum: PAY_LATER value ──────────────────────────────
    # ALTER TYPE ADD VALUE cannot run inside a transaction block in PostgreSQL.
    # We must temporarily switch to AUTOCOMMIT for this single statement.
    bind = op.get_bind()

    # Check if the enum value already exists — skip if so
    result = bind.execute(text(
        "SELECT 1 FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'paymentmethod' AND e.enumlabel = 'PAY_LATER'"
    ))
    if not result.fetchone():
        # Commit the current transaction first, then run in AUTOCOMMIT
        bind.execute(text("COMMIT"))
        bind.execute(text("ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'PAY_LATER'"))
        # Start a new transaction so Alembic's cleanup doesn't crash
        bind.execute(text("BEGIN"))

    # ── paymentstatus enum: CANCELLED value ──────────────────────────────
    result2 = bind.execute(text(
        "SELECT 1 FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'paymentstatus' AND e.enumlabel = 'CANCELLED'"
    ))
    if not result2.fetchone():
        bind.execute(text("COMMIT"))
        bind.execute(text("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'CANCELLED'"))
        bind.execute(text("BEGIN"))


def downgrade():
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at")
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at")
    # Note: cannot remove enum values in PostgreSQL without recreating the type
