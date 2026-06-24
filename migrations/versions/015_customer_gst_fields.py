"""Add GST fields to customers table

Revision ID: 015_customer_gst_fields
Revises: 014_domain_service_overrides
Create Date: 2026-06-16

Adds gst_number, gst_name, gst_address to customers table.
These were present in the model but never migrated.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '015_customer_gst_fields'
down_revision = '014_domain_service_overrides'
branch_labels = None
depends_on = None


def col_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade() -> None:
    # ── customers: add GST columns ──────────────────────────────
    if not col_exists('customers', 'gst_number'):
        op.add_column('customers', sa.Column('gst_number', sa.String(20), nullable=True))
    if not col_exists('customers', 'gst_name'):
        op.add_column('customers', sa.Column('gst_name', sa.String(200), nullable=True))
    if not col_exists('customers', 'gst_address'):
        op.add_column('customers', sa.Column('gst_address', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'gst_address')
    op.drop_column('customers', 'gst_name')
    op.drop_column('customers', 'gst_number')
