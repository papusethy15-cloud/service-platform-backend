"""Add missing updated_at column to quotation_appliances

Revision ID: 043
Revises: 042
Create Date: 2026-07-06

The quotation_appliances table was created in migration 019 without an
updated_at column. The QuotationAppliance SQLAlchemy model inherits from
BaseModel which declares updated_at, so every SELECT on this table raises
a column-not-found error in SQLAlchemy. That error is silently swallowed
by the bare `except Exception` in get_quotation(), causing the frontend to
always see appliances=[] and display "No appliances yet" even when appliances
have been saved successfully.
"""
from alembic import op
import sqlalchemy as sa

revision = '043'
down_revision = '042'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'quotation_appliances',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=True,
        )
    )


def downgrade():
    op.drop_column('quotation_appliances', 'updated_at')
