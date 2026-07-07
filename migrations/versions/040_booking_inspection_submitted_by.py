"""040_booking_inspection_submitted_by

Add inspection_submitted_by column to bookings table.

This column tracks WHO submitted the inspection report for a booking:
  - 'TECHNICIAN' — submitted by the assigned technician via captain app
  - 'CCO'        — submitted by a CCO on behalf of the technician via CCO portal
  - 'ADMIN'      — submitted by an admin via the admin dashboard
  - NULL         — inspection not yet submitted

When non-NULL and not 'TECHNICIAN', the captain app hides its
inspection form and shows a read-only view of the submitted report.

Revision ID: 040
Revises: 039
"""
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS inspection_submitted_by VARCHAR(20)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS inspection_submitted_by")
