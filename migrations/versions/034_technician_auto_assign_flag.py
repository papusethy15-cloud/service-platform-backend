"""
034_technician_auto_assign_flag

Adds:
  - auto_assign_eligible column to technicians (default True, backfilled True
    for all existing rows so behaviour is unchanged until admin opts a
    technician out of auto-assign)

Revision ID: 034
Revises: 033
"""
from alembic import op
import sqlalchemy as sa

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "technicians",
        sa.Column("auto_assign_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Drop the server_default after backfill so future inserts rely on the
    # application-level default (keeps behaviour identical, avoids a
    # dangling server default that could mask future explicit values).
    op.alter_column("technicians", "auto_assign_eligible", server_default=None)


def downgrade() -> None:
    op.drop_column("technicians", "auto_assign_eligible")
