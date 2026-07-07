"""Add pre_reschedule_status to bookings table

Revision ID: 041_add_pre_reschedule_status
Revises: 040_booking_inspection_submitted_by
Create Date: 2026-07-05

When a booking is rescheduled mid-repair (from INSPECTING, IN_PROGRESS, etc.),
we now store the status the booking was in immediately before RESCHEDULED.
This lets the technician app, CCO portal, and admin dashboard resume the
booking at the correct repair stage after the rescheduled visit, instead of
treating every rescheduled booking as a brand-new visit.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '041'
down_revision = '040'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'bookings',
        sa.Column('pre_reschedule_status', sa.String(30), nullable=True),
    )


def downgrade():
    op.drop_column('bookings', 'pre_reschedule_status')
