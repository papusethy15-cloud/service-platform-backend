"""021 — Add PAID, CLOSED, SETTLED, QUOTATION_APPROVED to booking status enum

Revision ID: 021_booking_status_paid_closed
Revises: 020_quotation_tax_mode
Create Date: 2026-06-18
"""
from alembic import op

revision = '021_booking_status_paid_closed'
down_revision = '020'
branch_labels = None
depends_on = None

# Only truly NEW statuses — INVOICE_GENERATED and PAYMENT_PENDING already existed
NEW_STATUSES = ['PAID', 'CLOSED', 'SETTLED', 'QUOTATION_APPROVED']

def upgrade():
    for status in NEW_STATUSES:
        op.execute(f"ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS '{status}'")

def downgrade():
    pass  # PostgreSQL enum values cannot be removed without recreating the type
