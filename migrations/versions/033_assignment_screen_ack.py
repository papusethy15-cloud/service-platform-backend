"""
033_assignment_screen_ack

Adds:
  - SCREEN_MISSED value to assignmentstatus enum
  - screen_shown_at column to assignment_history

Revision ID: 033
Revises: 032_user_firebase_uid
"""
from alembic import op
import sqlalchemy as sa

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add new enum value (PostgreSQL requires ALTER TYPE ... ADD VALUE)
    op.execute("ALTER TYPE assignmentstatus ADD VALUE IF NOT EXISTS 'SCREEN_MISSED'")

    # 2. Add screen_shown_at column to assignment_history
    op.add_column(
        "assignment_history",
        sa.Column("screen_shown_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Remove column (cannot remove enum value in Postgres without recreating type)
    op.drop_column("assignment_history", "screen_shown_at")
