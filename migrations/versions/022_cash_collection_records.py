"""022 cash collection records

Revision ID: 022_cash_collection_records
Revises: 021_booking_status_paid_closed
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '022_cash_collection_records'
down_revision = '021_booking_status_paid_closed'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add new columns to payment_transactions
    op.add_column('payment_transactions',
        sa.Column('collected_by_role', sa.String(30), nullable=True)
    )

    # Create enum types
    cash_collection_status = postgresql.ENUM(
        'PENDING', 'COLLECTED',
        name='cashcollectionstatus',
        create_type=True
    )
    cash_collection_status.create(op.get_bind(), checkfirst=True)

    op.add_column('payment_transactions',
        sa.Column('cash_collection_status',
                  sa.Enum('PENDING', 'COLLECTED', name='cashcollectionstatus'),
                  nullable=True)
    )

    # 2. Create cash_collection_records table
    op.create_table(
        'cash_collection_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('payment_transaction_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('payment_transactions.id'), nullable=False, unique=True),
        sa.Column('booking_id',    postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'),    nullable=False),
        sa.Column('invoice_id',    postgresql.UUID(as_uuid=True), sa.ForeignKey('invoices.id'),    nullable=False),
        sa.Column('technician_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('technicians.id'), nullable=False),
        sa.Column('customer_id',   postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id'),   nullable=False),
        sa.Column('amount',   sa.Float, nullable=False),
        sa.Column('status',   sa.Enum('PENDING', 'COLLECTED', name='cashcollectionstatus'), nullable=False, server_default='PENDING'),
        sa.Column('collected_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('collected_at', sa.DateTime, nullable=True),
        sa.Column('notes',     sa.Text, nullable=True),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_cash_collection_technician', 'cash_collection_records', ['technician_id'])
    op.create_index('ix_cash_collection_status',     'cash_collection_records', ['status'])


def downgrade():
    op.drop_index('ix_cash_collection_status',     table_name='cash_collection_records')
    op.drop_index('ix_cash_collection_technician', table_name='cash_collection_records')
    op.drop_table('cash_collection_records')
    op.drop_column('payment_transactions', 'cash_collection_status')
    op.drop_column('payment_transactions', 'collected_by_role')
    op.execute("DROP TYPE IF EXISTS cashcollectionstatus")
