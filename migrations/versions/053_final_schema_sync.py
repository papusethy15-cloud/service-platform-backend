"""053_final_schema_sync

ROOT CAUSE of why 051/052 DDL never persisted on VPS:
  Both 051 and 052 used `op.get_bind()` and called `bind.execute(text(stmt))`.
  Under asyncpg's run_sync bridge, the connection is already wrapped in an
  implicit transaction managed by Alembic's `context.begin_transaction()`.
  In this context, PostgreSQL DDL (ALTER TABLE) IS transactional, but the
  `connection.commit()` call in _maybe_stamp_baseline() operates on the
  OUTER connection — not the migration's inner transaction — causing asyncpg
  to raise or silently discard the DDL when the outer transaction is
  committed/rolled-back separately.

  Result: Alembic records 051/052 as applied (stamps them), but the ALTER
  TABLE statements never actually committed to PostgreSQL.

THIS MIGRATION:
  - Uses `op.execute(text(stmt))` — Alembic's own DDL execution path,
    which correctly participates in the transaction that Alembic controls.
  - ALL statements use IF NOT EXISTS / safe guards — complete no-op on any
    DB that already has the columns.
  - env.py FINAL_MIGRATION updated to '053', STAMP_AT to '052'.

DO NOT manually stamp this on VPS. Push the code and let pm2 restart
trigger _auto_migrate() → env.py will reset alembic_version to '052' →
alembic upgrade head will execute this migration.

Revision ID: 053
Revises: 052
Create Date: 2026-07-07 (IST)
"""
from alembic import op
from sqlalchemy import text

revision = '053'
down_revision = '052'
branch_labels = None
depends_on = None


def upgrade():
    # ── technicians ──────────────────────────────────────────────────────────
    # Using op.execute() — NOT op.get_bind().execute() — so DDL participates
    # in Alembic's transaction correctly under asyncpg run_sync bridge.
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS is_online                BOOLEAN NOT NULL DEFAULT FALSE"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS fcm_token                VARCHAR(500)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lat                 DOUBLE PRECISION"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lng                 DOUBLE PRECISION"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_at             TIMESTAMP WITH TIME ZONE"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS auto_assign_eligible     BOOLEAN NOT NULL DEFAULT TRUE"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS alternate_mobile         VARCHAR(20)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS dob                      DATE"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS gender                   VARCHAR(10)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS pincode                  VARCHAR(10)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_type            VARCHAR(50)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_number          VARCHAR(50)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_name   VARCHAR(150)"))
    op.execute(text("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_mobile VARCHAR(20)"))

    # ── users ─────────────────────────────────────────────────────────────────
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token           VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid        VARCHAR(128)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url        VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url   VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type       VARCHAR(50)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type  VARCHAR(50)"))

    # ── services ──────────────────────────────────────────────────────────────
    op.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_pending_verify  INTEGER NOT NULL DEFAULT 0"))
    op.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS suggested_by_tech  UUID"))

    # ── quotation_service_items ───────────────────────────────────────────────
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS is_pending_verify        INTEGER NOT NULL DEFAULT 0"))
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS custom_service_name      TEXT"))
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS tech_commission_override DOUBLE PRECISION"))

    # ── payment_transactions ──────────────────────────────────────────────────
    op.execute(text("ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS due_collect_at   TIMESTAMP WITH TIME ZONE"))
    op.execute(text("ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP WITH TIME ZONE"))

    # Make quotation_service_items.service_id nullable (custom services have no FK).
    # Use op.get_bind() ONLY for SELECT queries (read-only) — DDL goes through op.execute().
    bind = op.get_bind()
    row = bind.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='quotation_service_items' AND column_name='service_id'"
    )).fetchone()
    if row and row[0] == 'NO':
        op.execute(text("ALTER TABLE quotation_service_items ALTER COLUMN service_id DROP NOT NULL"))

    # Unique constraint on users.firebase_uid (idempotent)
    exists = bind.execute(text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='uq_users_firebase_uid'"
    )).fetchone()
    if not exists:
        try:
            op.execute(text("ALTER TABLE users ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid)"))
        except Exception:
            pass

    print("[053] Final schema sync complete — all missing columns added via op.execute().")


def downgrade():
    # Intentionally minimal — these columns are required by live models.
    op.execute(text("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at"))
    op.execute(text("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify"))
    op.execute(text("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech"))
    op.execute(text("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify"))
