"""
036_pay_later_proper

Adds:
  - PAY_LATER value to paymentmethod enum (replaces the old
    reference_number == 'PAY_LATER' string-sentinel hack)
  - due_collect_at column on payment_transactions (when the customer
    promised to pay by — drives the collection reminder sweep)
  - last_reminder_at column on payment_transactions (last time a
    collect-payment reminder was sent, so the sweep can re-remind daily)

Revision ID: 036
Revises: 035
"""
from alembic import op
import sqlalchemy as sa

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'PAY_LATER'")
    op.add_column("payment_transactions", sa.Column("due_collect_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_transactions", sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_transactions", "last_reminder_at")
    op.drop_column("payment_transactions", "due_collect_at")
    # Cannot remove enum value in Postgres without recreating the type.
