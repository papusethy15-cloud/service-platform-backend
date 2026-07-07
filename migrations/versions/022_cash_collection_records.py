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
    bind = op.get_bind()

    # ── payment_transactions: add columns idempotently ────────────────────────
    bind.execute(sa.text("""
        ALTER TABLE payment_transactions
            ADD COLUMN IF NOT EXISTS collected_by_role VARCHAR(30)
    """))

    # Create enum type if not exists
    bind.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE cashcollectionstatus AS ENUM ('PENDING', 'COLLECTED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    bind.execute(sa.text("""
        ALTER TABLE payment_transactions
            ADD COLUMN IF NOT EXISTS cash_collection_status cashcollectionstatus
    """))

    # ── cash_collection_records table ─────────────────────────────────────────
    table_exists = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='cash_collection_records')"
    )).scalar()

    if not table_exists:
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
            sa.Column('amount',   sa.Float,   nullable=False),
            sa.Column('status',   sa.Enum('PENDING', 'COLLECTED', name='cashcollectionstatus'),
                      nullable=False, server_default='PENDING'),
            sa.Column('collected_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('collected_at', sa.DateTime, nullable=True),
            sa.Column('notes',     sa.Text,    nullable=True),
            sa.Column('is_active', sa.Boolean, nullable=False, server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )

    # Indexes — use IF NOT EXISTS (Postgres 9.5+)
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cash_collection_technician ON cash_collection_records (technician_id)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cash_collection_status ON cash_collection_records (status)"
    ))


def downgrade():
    op.drop_index('ix_cash_collection_status',     table_name='cash_collection_records')
    op.drop_index('ix_cash_collection_technician', table_name='cash_collection_records')
    op.drop_table('cash_collection_records')
    op.drop_column('payment_transactions', 'cash_collection_status')
    op.drop_column('payment_transactions', 'collected_by_role')
    op.execute("DROP TYPE IF EXISTS cashcollectionstatus")
