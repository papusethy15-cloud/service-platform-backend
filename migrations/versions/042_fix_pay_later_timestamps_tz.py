"""
042_fix_pay_later_timestamps_tz

ALTER due_collect_at and last_reminder_at on payment_transactions
from TIMESTAMP WITHOUT TIME ZONE → TIMESTAMP WITH TIME ZONE.

These columns were created in 036 without timezone=True, causing a
ROLLBACK when a timezone-aware datetime (from ISO 8601 frontend input)
was written to them in PostgreSQL strict mode.

Revision ID: 042
Revises: 041
"""
from alembic import op
import sqlalchemy as sa

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "payment_transactions",
        "due_collect_at",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=True,
        postgresql_using="due_collect_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "payment_transactions",
        "last_reminder_at",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=True,
        postgresql_using="last_reminder_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    op.alter_column(
        "payment_transactions",
        "due_collect_at",
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )
    op.alter_column(
        "payment_transactions",
        "last_reminder_at",
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )
