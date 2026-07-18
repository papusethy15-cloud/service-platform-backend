"""Add customer review fields to bookings table

Revision ID: 074_add_customer_review_fields
Revises: 073_cco_attendance_and_salary
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '074_add_customer_review_fields'
down_revision = '073_cco_attendance_and_salary'
branch_labels = None
depends_on = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    result = bind.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c)"
    ), {"t": table_name, "c": column_name})
    return result.scalar()


def upgrade():
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_rating FLOAT"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_review TEXT"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_name   VARCHAR(120)"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_city   VARCHAR(80)"))


def downgrade():
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS customer_city"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS customer_name"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS customer_review"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS customer_rating"))
