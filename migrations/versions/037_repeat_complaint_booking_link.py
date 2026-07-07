"""
037_repeat_complaint_booking_link

Adds:
  - repeat_of_booking_id column on bookings (nullable FK -> bookings.id).
    Set when a booking is created via POST /bookings/{id}/report-issue
    (customer reports an issue within 10 days of the original booking's
    closure). Links the new repeat-complaint booking back to the original
    so settlement (see settle_booking in routes/bookings.py) can resolve
    the technician who did the original job and apply the repeat-complaint
    penalty / cross-technician compensation logic.

Revision ID: 037
Revises: 036
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("repeat_of_booking_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_bookings_repeat_of_booking_id",
        "bookings", "bookings",
        ["repeat_of_booking_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_repeat_of_booking_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "repeat_of_booking_id")
