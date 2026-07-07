"""052_ensure_all_columns_final

PURPOSE:
  Permanent safety-net migration. All ADD COLUMN statements use IF NOT EXISTS
  so this is a complete no-op on any DB that already has the columns (local dev,
  any VPS that successfully ran 051).

  On a VPS where 051 ran but some columns were still missing (e.g. due to the
  connection.commit() bug in earlier env.py versions that silently rolled back
  DDL), this migration will add them.

  env.py FINAL_MIGRATION is now '052', STAMP_AT is '051'.
  The _maybe_stamp_baseline() guard will reset alembic_version to '051' on any
  VPS that does not yet have '052' stamped, then upgrade() runs this migration.

DO NOT manually stamp this on VPS.

Revision ID: 052
Revises: 051
Create Date: 2026-07-07 (IST)
"""
from alembic import op
from sqlalchemy import text

revision = '052'
down_revision = '051'
branch_labels = None
depends_on = None


# Every column the ORM models require — all IF NOT EXISTS, safe to re-run.
_DDL = [
    # ── technicians ──────────────────────────────────────────────────────────
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS is_online                BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS fcm_token                VARCHAR(500)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lat                 DOUBLE PRECISION",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lng                 DOUBLE PRECISION",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_at             TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS auto_assign_eligible     BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS alternate_mobile         VARCHAR(20)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS dob                      DATE",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS gender                   VARCHAR(10)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS pincode                  VARCHAR(10)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_type            VARCHAR(50)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_number          VARCHAR(50)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_name   VARCHAR(150)",
    "ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_mobile VARCHAR(20)",

    # ── users ─────────────────────────────────────────────────────────────────
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token           VARCHAR(500)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid        VARCHAR(128)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url        VARCHAR(500)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url   VARCHAR(500)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type       VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type  VARCHAR(50)",

    # ── services ──────────────────────────────────────────────────────────────
    "ALTER TABLE services ADD COLUMN IF NOT EXISTS is_pending_verify  INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE services ADD COLUMN IF NOT EXISTS suggested_by_tech  UUID",

    # ── quotation_service_items ───────────────────────────────────────────────
    "ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS is_pending_verify        INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS custom_service_name      TEXT",
    "ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS tech_commission_override DOUBLE PRECISION",

    # ── payment_transactions ──────────────────────────────────────────────────
    "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS due_collect_at   TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP WITH TIME ZONE",
]


def upgrade():
    bind = op.get_bind()

    for stmt in _DDL:
        bind.execute(text(stmt))

    # Make quotation_service_items.service_id nullable (custom services have no FK)
    row = bind.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='quotation_service_items' AND column_name='service_id'"
    )).fetchone()
    if row and row[0] == 'NO':
        bind.execute(text(
            "ALTER TABLE quotation_service_items ALTER COLUMN service_id DROP NOT NULL"
        ))

    # Unique constraint on users.firebase_uid (idempotent)
    exists = bind.execute(text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='uq_users_firebase_uid'"
    )).fetchone()
    if not exists:
        try:
            bind.execute(text(
                "ALTER TABLE users ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid)"
            ))
        except Exception:
            pass

    print("[052] All columns verified / added. VPS schema is now fully in sync.")


def downgrade():
    # Intentionally minimal — these columns are required by live models.
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at")
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify")
