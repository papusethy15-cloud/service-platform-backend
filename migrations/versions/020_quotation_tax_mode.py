"""Add tax_mode, customer GST snapshot to quotations

Revision ID: 020
Revises: 019
"""
from alembic import op
import sqlalchemy as sa

revision = '020'
down_revision = '019'
branch_labels = None
depends_on = None

def upgrade():
    # tax_mode: NONE | B2C | B2B  (default B2C = tax enabled, customer type consumer)
    op.add_column('quotations', sa.Column('tax_mode', sa.String(10), nullable=False, server_default='B2C'))
    # B2B customer GST snapshot
    op.add_column('quotations', sa.Column('customer_gst_number', sa.String(20), nullable=True))
    op.add_column('quotations', sa.Column('customer_gst_name',   sa.String(200), nullable=True))
    op.add_column('quotations', sa.Column('customer_gst_address',sa.Text(), nullable=True))

def downgrade():
    op.drop_column('quotations', 'customer_gst_address')
    op.drop_column('quotations', 'customer_gst_name')
    op.drop_column('quotations', 'customer_gst_number')
    op.drop_column('quotations', 'tax_mode')
