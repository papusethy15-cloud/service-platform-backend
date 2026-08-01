"""Fix commissions table — drop stale 'amount NOT NULL' column from migration 001

Background
----------
Migration 001_add_all_missing_tables.py originally created `commissions` with:
    amount          FLOAT NOT NULL
    commission_type VARCHAR(30)
    is_active       BOOLEAN
    updated_at      DATETIME

The Commission model was later redesigned: `amount` was split into
`base_amount` + `commission_amount`, and `commission_type` / `is_active` /
`updated_at` were removed from the model.  The new columns were added by
migration 003 (base_amount) and 038, but the OLD `amount NOT NULL` column
was never dropped from the VPS database.

This causes the error on every /settle call:
    asyncpg.exceptions.NotNullViolationError:
    null value in column "amount" of relation "commissions"
    violates not-null constraint

Fix: drop the four stale columns with safe IF EXISTS guards.

Revision ID: 080
Revises: 079
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa

revision = '080'
down_revision = '079'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Helper — only drop if the column actually exists (idempotent)
    def drop_if_exists(table: str, column: str):
        exists = conn.execute(sa.text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = :t AND column_name = :c
        """), {"t": table, "c": column}).fetchone()
        if exists:
            op.drop_column(table, column)
            print(f"  dropped {table}.{column}")
        else:
            print(f"  skip   {table}.{column} (not present)")

    print("080: fixing commissions table — dropping stale columns from migration 001")
    drop_if_exists('commissions', 'amount')           # THE bug: NOT NULL, never written
    drop_if_exists('commissions', 'commission_type')  # orphan from 001
    drop_if_exists('commissions', 'is_active')        # orphan from 001
    drop_if_exists('commissions', 'updated_at')       # orphan from 001


def downgrade():
    # Restore the columns exactly as migration 001 created them so rollback works
    op.add_column('commissions', sa.Column('amount',          sa.Float(),   nullable=True))
    op.add_column('commissions', sa.Column('commission_type', sa.String(30)))
    op.add_column('commissions', sa.Column('is_active',       sa.Boolean(), server_default='true'))
    op.add_column('commissions', sa.Column('updated_at',      sa.DateTime(timezone=True)))
    # Re-apply NOT NULL only if you truly need it (probably not — this is a rollback escape hatch)
