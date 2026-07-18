"""add salary_settlements table

Revision ID: 072_add_salary_settlements
Revises: 071_add_is_salary_group
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '072_add_salary_settlements'
down_revision = '071_add_is_salary_group'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    from sqlalchemy import text as _text
    exists = bind.execute(_text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='salary_settlements')"
    )).scalar()
    if exists:
        print("[072] salary_settlements already exists — skipping")
        return
    op.create_table(
        'salary_settlements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('technician_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('technicians.id', ondelete='CASCADE'), nullable=False),
        sa.Column('commission_group_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('commission_groups.id', ondelete='SET NULL'), nullable=True),
        sa.Column('month', sa.Integer(), nullable=False),   # 1-12
        sa.Column('year',  sa.Integer(), nullable=False),

        # Salary structure (copied from group at time of settlement; admin may edit)
        sa.Column('monthly_salary',   sa.Float(), nullable=False, server_default='0'),
        sa.Column('petrol_amount',    sa.Float(), nullable=False, server_default='0'),
        sa.Column('mobile_recharge',  sa.Float(), nullable=False, server_default='0'),
        sa.Column('bonus_amount',     sa.Float(), nullable=False, server_default='0'),
        sa.Column('hra_amount',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('other_allowances', sa.Float(), nullable=False, server_default='0'),
        sa.Column('deductions',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('deduction_notes',  sa.String(500), nullable=True),

        # Computed totals
        sa.Column('market_reimbursement', sa.Float(), nullable=False, server_default='0'),
        sa.Column('gross_salary',         sa.Float(), nullable=False, server_default='0'),
        sa.Column('net_salary',           sa.Float(), nullable=False, server_default='0'),  # gross - deductions + reimbursement

        # Stats snapshot
        sa.Column('total_bookings',    sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_hours_worked', sa.Float(),  nullable=False, server_default='0'),
        sa.Column('attendance_days',   sa.Integer(), nullable=False, server_default='0'),

        # Status / payment
        sa.Column('status', sa.String(30), nullable=False, server_default='GENERATED'),  # GENERATED | PAID | SENT_TO_BANK
        sa.Column('admin_notes', sa.String(500), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('wallet_txn_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('wallet_transactions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('payment_reference', sa.String(300), nullable=True),
        sa.Column('payout_method', sa.String(30), nullable=True),  # UPI | BANK

        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_salary_settlements_technician_month_year',
                    'salary_settlements', ['technician_id', 'month', 'year'])


def downgrade():
    op.drop_index('ix_salary_settlements_technician_month_year',
                  table_name='salary_settlements')
    op.drop_table('salary_settlements')
