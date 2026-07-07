"""
035_booking_cancellation_verification

Adds:
  - CANCELLATION_REQUESTED value to bookingstatus enum (customer/technician
    initiated cancellations now go through this pending state instead of
    landing directly on CANCELLED; admin/CCO must confirm or reject it)
  - pre_cancel_status column on bookings (stores the status to restore to if
    admin/CCO rejects the cancellation request)

Revision ID: 035
Revises: 034
"""
from alembic import op
import sqlalchemy as sa

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CANCELLATION_REQUESTED'")
    op.add_column(
        "bookings",
        sa.Column("pre_cancel_status", sa.String(length=30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "pre_cancel_status")
    # Cannot remove enum value in Postgres without recreating the type.
