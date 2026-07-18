"""Add is_active column to quotation_part_items

Revision ID: 070
Revises: 069
Create Date: 2026-07-15

Root cause fix:
  QuotationPartItem model referenced is_active in WHERE clauses across the
  codebase (list_market_purchase_verifications, delete_part_from_quotation,
  quotation serialisers, etc.) but the column was never created in the DB.
  This caused:
    - Market Purchase tab in admin dashboard always returning 0 rows
      (SQLAlchemy WHERE None = true -> no rows ever matched)
    - Soft-delete (part.is_active = False) silently failing
    - All is_active filters on QuotationPartItem returning wrong results

  Fix: ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE
  Existing rows default to TRUE (all existing parts treated as active).
"""

from alembic import op
from sqlalchemy import text

revision = '070'
down_revision = '069'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(text("""
        ALTER TABLE quotation_part_items
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
    """))
    op.execute(text("""
        UPDATE quotation_part_items SET is_active = TRUE WHERE is_active IS NULL;
    """))


def downgrade():
    op.execute(text("""
        ALTER TABLE quotation_part_items DROP COLUMN IF EXISTS is_active;
    """))
