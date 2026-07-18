"""Add CCO attendance table, CCO salary fields, and CCO salary settlements

Revision ID: 073_cco_attendance_and_salary
Revises: 072_add_salary_settlements
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '073_cco_attendance_and_salary'
down_revision = '072_add_salary_settlements'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. CCO Attendance table ───────────────────────────────────────────────
    op.create_table(
        'cco_attendance',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('check_in', sa.DateTime(timezone=True), nullable=True),
        sa.Column('check_out', sa.DateTime(timezone=True), nullable=True),
        # accumulated seconds across all sessions of the day (for multi-login days)
        sa.Column('accumulated_seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False, server_default='PRESENT'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('user_id', 'date', name='uq_cco_attendance_user_date'),
    )
    op.create_index('ix_cco_attendance_user_id', 'cco_attendance', ['user_id'])
    op.create_index('ix_cco_attendance_date',    'cco_attendance', ['date'])

    # ── 2. CCO Salary Settlements table ──────────────────────────────────────
    op.create_table(
        'cco_salary_settlements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('year',  sa.Integer(), nullable=False),
        # Salary structure
        sa.Column('monthly_salary',   sa.Float(), nullable=False, server_default='0'),
        sa.Column('petrol_amount',    sa.Float(), nullable=False, server_default='0'),
        sa.Column('mobile_recharge',  sa.Float(), nullable=False, server_default='0'),
        sa.Column('bonus_amount',     sa.Float(), nullable=False, server_default='0'),
        sa.Column('hra_amount',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('other_allowances', sa.Float(), nullable=False, server_default='0'),
        sa.Column('deductions',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('deduction_notes',  sa.String(500), nullable=True),
        # Attendance summary (captured at time of generation)
        sa.Column('total_days',       sa.Integer(), nullable=False, server_default='0'),
        sa.Column('present_days',     sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_hours',      sa.Float(),   nullable=False, server_default='0'),
        # Computed net
        sa.Column('gross_salary',     sa.Float(), nullable=False, server_default='0'),
        sa.Column('net_salary',       sa.Float(), nullable=False, server_default='0'),
        # Payment
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),  # PENDING, PAID
        sa.Column('payment_method',   sa.String(20), nullable=True),   # UPI, BANK
        sa.Column('payment_ref',      sa.String(200), nullable=True),
        sa.Column('paid_at',          sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_by',          postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('salary_notes',     sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('user_id', 'month', 'year', name='uq_cco_salary_user_month_year'),
    )
    op.create_index('ix_cco_salary_user_id', 'cco_salary_settlements', ['user_id'])

    # ── 3. Add payout / salary fields to users table (for CCO) ───────────────
    op.add_column('users', sa.Column('payout_upi_id',         sa.String(200), nullable=True))
    op.add_column('users', sa.Column('payout_bank_account',   sa.String(100), nullable=True))
    op.add_column('users', sa.Column('payout_bank_ifsc',      sa.String(20),  nullable=True))
    op.add_column('users', sa.Column('payout_bank_name',      sa.String(100), nullable=True))
    op.add_column('users', sa.Column('payout_account_holder', sa.String(150), nullable=True))
    # CCO salary structure stored on the user (admin sets via CCO management)
    op.add_column('users', sa.Column('monthly_salary',   sa.Float(), nullable=True))
    op.add_column('users', sa.Column('petrol_amount',    sa.Float(), nullable=True))
    op.add_column('users', sa.Column('mobile_recharge',  sa.Float(), nullable=True))
    op.add_column('users', sa.Column('bonus_amount',     sa.Float(), nullable=True))
    op.add_column('users', sa.Column('hra_amount',       sa.Float(), nullable=True))
    op.add_column('users', sa.Column('other_allowances', sa.Float(), nullable=True))
    op.add_column('users', sa.Column('salary_notes',     sa.Text(), nullable=True))


def downgrade():
    for col in ['salary_notes', 'other_allowances', 'hra_amount', 'bonus_amount',
                'mobile_recharge', 'petrol_amount', 'monthly_salary',
                'payout_account_holder', 'payout_bank_name', 'payout_bank_ifsc',
                'payout_bank_account', 'payout_upi_id']:
        op.drop_column('users', col)
    op.drop_table('cco_salary_settlements')
    op.drop_table('cco_attendance')
