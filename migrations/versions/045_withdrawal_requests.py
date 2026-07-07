"""Add withdrawal_requests table

Revision ID: 045
Revises: 044
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '045'
down_revision = '044'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'withdrawal_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
        sa.Column('wallet_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('wallets.id'), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('upi_id', sa.String(200), nullable=True),
        sa.Column('bank_account', sa.String(200), nullable=True),
        sa.Column('bank_ifsc', sa.String(20), nullable=True),
        sa.Column('bank_name', sa.String(200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('admin_notes', sa.Text(), nullable=True),
        sa.Column('reviewed_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('wallet_txn_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('wallet_transactions.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.text('now()')),
    )
    op.create_index('ix_withdrawal_requests_technician_id', 'withdrawal_requests', ['technician_id'])
    op.create_index('ix_withdrawal_requests_status', 'withdrawal_requests', ['status'])


def downgrade():
    op.drop_index('ix_withdrawal_requests_status', 'withdrawal_requests')
    op.drop_index('ix_withdrawal_requests_technician_id', 'withdrawal_requests')
    op.drop_table('withdrawal_requests')
