"""Add missing public booking fields to bookings table

Revision ID: 016_booking_public_fields
Revises: 015_customer_gst_fields
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = '016_booking_public_fields'
down_revision = '015_customer_gst_fields'
branch_labels = None
depends_on = None

def col_exists(table, column):
    from sqlalchemy import inspect, text
    conn = op.get_bind()
    result = conn.execute(text(
        f"SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_name='{table}' AND column_name='{column}'"
    ))
    return result.scalar() > 0

def upgrade() -> None:
    if not col_exists('bookings', 'service_name'):
        op.add_column('bookings', sa.Column('service_name', sa.String(200), nullable=True))
    if not col_exists('bookings', 'address_line'):
        op.add_column('bookings', sa.Column('address_line', sa.Text(), nullable=True))
    if not col_exists('bookings', 'city'):
        op.add_column('bookings', sa.Column('city', sa.String(100), nullable=True))
    if not col_exists('bookings', 'pincode'):
        op.add_column('bookings', sa.Column('pincode', sa.String(10), nullable=True))

def downgrade() -> None:
    op.drop_column('bookings', 'pincode')
    op.drop_column('bookings', 'city')
    op.drop_column('bookings', 'address_line')
    op.drop_column('bookings', 'service_name')
