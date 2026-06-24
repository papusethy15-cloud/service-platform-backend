"""Add quotation_appliances table and repeat-complaint columns

Revision ID: 019
Revises: 018
Create Date: 2026-06-17

Adds:
  - quotation_appliances table (links customer appliance to quotation with repeat-complaint tracking)
  - appliance_label column to quotation_service_items
  - is_repeat_complaint column to quotation_service_items
  - is_repeat_complaint column to quotation_part_items
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '019'
down_revision = '018'
branch_labels = None
depends_on = None


def upgrade():
    # ── quotation_appliances table ──────────────────────────────────────────
    op.create_table(
        'quotation_appliances',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('quotation_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('quotations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('appliance_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('customer_appliances.id', ondelete='SET NULL'), nullable=True),
        sa.Column('appliance_label', sa.String(300), nullable=False),
        sa.Column('is_repeat_complaint', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('repeat_booking_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('bookings.id', ondelete='SET NULL'), nullable=True),
        sa.Column('repeat_confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
    )
    op.create_index('ix_quotation_appliances_quotation_id', 'quotation_appliances', ['quotation_id'])

    # ── quotation_service_items: add appliance_label + is_repeat_complaint ──
    op.add_column('quotation_service_items',
        sa.Column('appliance_label', sa.String(300), nullable=True))
    op.add_column('quotation_service_items',
        sa.Column('is_repeat_complaint', sa.Boolean(), nullable=False, server_default='false'))

    # ── quotation_part_items: add is_repeat_complaint ───────────────────────
    op.add_column('quotation_part_items',
        sa.Column('is_repeat_complaint', sa.Boolean(), nullable=False, server_default='false'))


def downgrade():
    op.drop_column('quotation_part_items', 'is_repeat_complaint')
    op.drop_column('quotation_service_items', 'is_repeat_complaint')
    op.drop_column('quotation_service_items', 'appliance_label')
    op.drop_index('ix_quotation_appliances_quotation_id', table_name='quotation_appliances')
    op.drop_table('quotation_appliances')
