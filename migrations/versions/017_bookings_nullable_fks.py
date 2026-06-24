"""Make bookings.address_id and service_id nullable for admin/CCO bookings

Revision ID: 017_bookings_nullable_fks
Revises: 016_booking_public_fields
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '017_bookings_nullable_fks'
down_revision = '016_booking_public_fields'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Drop NOT NULL constraints so admin can create bookings without address/service resolved yet
    op.alter_column('bookings', 'address_id',
                    existing_type=UUID(as_uuid=True),
                    nullable=True)
    op.alter_column('bookings', 'service_id',
                    existing_type=UUID(as_uuid=True),
                    nullable=True)

def downgrade() -> None:
    op.alter_column('bookings', 'service_id',
                    existing_type=UUID(as_uuid=True),
                    nullable=False)
    op.alter_column('bookings', 'address_id',
                    existing_type=UUID(as_uuid=True),
                    nullable=False)
