"""023 wallet_transactions add balance_before

Revision ID: 023_wallet_balance_before
Revises: 022_cash_collection_records
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa

revision = '023_wallet_balance_before'
down_revision = '022_cash_collection_records'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('wallet_transactions',
        sa.Column('balance_before', sa.Float, nullable=True)
    )

def downgrade():
    op.drop_column('wallet_transactions', 'balance_before')
