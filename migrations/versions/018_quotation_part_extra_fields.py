"""Add purchase_price, inventory_item_id, is_pending_verify to quotation_part_items

Revision ID: 018
Revises: 017
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = '018'
down_revision = '017_bookings_nullable_fks'
branch_labels = None
depends_on = None

def upgrade():
    # Add new columns with defaults so existing rows are unaffected
    op.add_column('quotation_part_items', sa.Column('purchase_price', sa.Float(), nullable=True, server_default='0'))
    op.add_column('quotation_part_items', sa.Column('inventory_item_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('quotation_part_items', sa.Column('is_pending_verify', sa.Integer(), nullable=True, server_default='0'))

def downgrade():
    op.drop_column('quotation_part_items', 'is_pending_verify')
    op.drop_column('quotation_part_items', 'inventory_item_id')
    op.drop_column('quotation_part_items', 'purchase_price')
