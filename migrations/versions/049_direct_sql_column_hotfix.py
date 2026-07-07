"""049_direct_sql_column_hotfix

Emergency re-application of all columns that 047+048 were stamped as
applied but never actually ran on the VPS DB.

NOTE: ALTER TYPE ADD VALUE cannot run inside a PostgreSQL transaction.
      Enum fixes (PAY_LATER, CANCELLED) are handled by the standalone
      script  scripts/fix_vps_schema.py  which uses AUTOCOMMIT mode.
      This migration ONLY does column additions (all IF NOT EXISTS).

Revision ID: 049
Revises: 048
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '049'
down_revision = '048'
branch_labels = None
depends_on = None


def _col_exists(conn, table, column):
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade():
    bind = op.get_bind()

    columns = [
        # technicians
        ("technicians", "is_online",                "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("technicians", "fcm_token",                "VARCHAR(500)"),
        ("technicians", "last_lat",                 "DOUBLE PRECISION"),
        ("technicians", "last_lng",                 "DOUBLE PRECISION"),
        ("technicians", "last_seen_at",             "TIMESTAMP WITH TIME ZONE"),
        ("technicians", "auto_assign_eligible",     "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("technicians", "alternate_mobile",         "VARCHAR(20)"),
        ("technicians", "dob",                      "DATE"),
        ("technicians", "gender",                   "VARCHAR(10)"),
        ("technicians", "pincode",                  "VARCHAR(10)"),
        ("technicians", "identity_type",            "VARCHAR(50)"),
        ("technicians", "identity_number",          "VARCHAR(50)"),
        ("technicians", "emergency_contact_name",   "VARCHAR(150)"),
        ("technicians", "emergency_contact_mobile", "VARCHAR(20)"),
        # users
        ("users", "fcm_token",          "VARCHAR(500)"),
        ("users", "firebase_uid",       "VARCHAR(128)"),
        ("users", "id_proof_url",       "VARCHAR(500)"),
        ("users", "address_proof_url",  "VARCHAR(500)"),
        ("users", "id_proof_type",      "VARCHAR(50)"),
        ("users", "address_proof_type", "VARCHAR(50)"),
        # services
        ("services", "is_pending_verify",  "INTEGER NOT NULL DEFAULT 0"),
        ("services", "suggested_by_tech",  "UUID"),
        # quotation_service_items
        ("quotation_service_items", "is_pending_verify",        "INTEGER NOT NULL DEFAULT 0"),
        ("quotation_service_items", "custom_service_name",      "TEXT"),
        ("quotation_service_items", "tech_commission_override",  "DOUBLE PRECISION"),
        # payment_transactions
        ("payment_transactions", "due_collect_at",   "TIMESTAMP WITH TIME ZONE"),
        ("payment_transactions", "last_reminder_at", "TIMESTAMP WITH TIME ZONE"),
    ]

    for table, col, coltype in columns:
        if not _col_exists(bind, table, col):
            bind.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {coltype}'))

    # Make service_id nullable on quotation_service_items
    result = bind.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='quotation_service_items' AND column_name='service_id'"
    ))
    row = result.fetchone()
    if row and row[0] == 'NO':
        bind.execute(text(
            "ALTER TABLE quotation_service_items ALTER COLUMN service_id DROP NOT NULL"
        ))

    # Unique constraint on users.firebase_uid (skip if exists)
    result = bind.execute(text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='uq_users_firebase_uid'"
    ))
    if not result.fetchone():
        try:
            bind.execute(text(
                "ALTER TABLE users ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid)"
            ))
        except Exception:
            pass


def downgrade():
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at")
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS address_proof_type")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS id_proof_type")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS address_proof_url")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS id_proof_url")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS firebase_uid")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS fcm_token")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS emergency_contact_mobile")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS emergency_contact_name")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS identity_number")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS identity_type")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS pincode")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS gender")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS dob")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS alternate_mobile")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS auto_assign_eligible")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS last_seen_at")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS last_lng")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS last_lat")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS fcm_token")
    op.execute("ALTER TABLE technicians DROP COLUMN IF EXISTS is_online")
