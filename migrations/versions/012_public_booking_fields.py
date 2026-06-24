"""Add public booking fields (service_name, address_line, city, pincode) and make service_id/address_id nullable

Revision ID: 012_public_booking_fields
Revises: fc36bebf9204
Create Date: 2026-06-15

These changes support website booking forms where customers submit free-text
service name and address rather than FK-linked IDs.
Admin resolves the FK references later from the admin dashboard.
"""
from alembic import op
import sqlalchemy as sa

revision = '012_public_booking_fields'
down_revision = 'fc36bebf9204'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make service_id and address_id nullable (public bookings don't have FKs)
    op.alter_column('bookings', 'service_id', nullable=True)
    op.alter_column('bookings', 'address_id', nullable=True)

    # Add free-text fields for public bookings
    op.add_column('bookings', sa.Column('service_name', sa.String(200), nullable=True))
    op.add_column('bookings', sa.Column('address_line', sa.Text(), nullable=True))
    op.add_column('bookings', sa.Column('city', sa.String(100), nullable=True))
    op.add_column('bookings', sa.Column('pincode', sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'pincode')
    op.drop_column('bookings', 'city')
    op.drop_column('bookings', 'address_line')
    op.drop_column('bookings', 'service_name')
    op.alter_column('bookings', 'address_id', nullable=False)
    op.alter_column('bookings', 'service_id', nullable=False)
