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

Fix: drop the four stale columns using raw SQL with IF EXISTS guards.
     Uses op.execute() instead of op.get_bind() to avoid Alembic 1.10+
     deprecation abort on asyncpg backends.

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
    # Use raw SQL with IF EXISTS — fully idempotent, no op.get_bind() needed.
    # Each ALTER TABLE is safe to run even if the column doesn't exist.
    op.execute("ALTER TABLE commissions DROP COLUMN IF EXISTS amount")
    op.execute("ALTER TABLE commissions DROP COLUMN IF EXISTS commission_type")
    op.execute("ALTER TABLE commissions DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE commissions DROP COLUMN IF EXISTS updated_at")


def downgrade():
    # Restore the columns exactly as migration 001 created them so rollback works
    op.add_column('commissions', sa.Column('amount',          sa.Float(),   nullable=True))
    op.add_column('commissions', sa.Column('commission_type', sa.String(30)))
    op.add_column('commissions', sa.Column('is_active',       sa.Boolean(), server_default='true'))
    op.add_column('commissions', sa.Column('updated_at',      sa.DateTime(timezone=True)))
