"""051_vps_definitive_column_fix

ROOT CAUSE (why 047/048/049/050 never actually ran on VPS):
  Each time a hotfix migration was created, someone manually ran
  `alembic stamp <revision>` on the VPS BEFORE pushing the migration file.
  Alembic then sees the revision as already applied and skips it.
  The `_maybe_stamp_baseline()` guard in env.py checked for the previous
  FINAL_MIGRATION (050) — once 050 was manually stamped, the guard
  returned early and never reset alembic_version.

  Result: VPS always reports "[OK] Auto-migrate: all Alembic migrations
  applied (head)" but the actual column DDL never executed.

THIS MIGRATION:
  - Is the authoritative final fix.
  - env.py FINAL_MIGRATION is updated to '051', STAMP_AT to '050'.
  - All ADD COLUMN statements use IF NOT EXISTS (safe no-op if already present).
  - Covers every column that the model definitions require but that may be
    absent on the VPS: technicians.is_online, technicians.last_seen_at,
    technicians.auto_assign_eligible, services.is_pending_verify,
    services.suggested_by_tech, and all related fields.

DO NOT manually stamp this on VPS. Push the code and let pm2 restart
trigger _auto_migrate() → env.py will reset alembic_version to '050' →
alembic upgrade head will execute this migration's upgrade().

Revision ID: 051
Revises: 050
Create Date: 2026-07-07 (IST)
"""
from alembic import op
from sqlalchemy import text

revision = '051'
down_revision = '050'
branch_labels = None
depends_on = None


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

    print("[051] VPS definitive column fix applied — all missing columns now present.")


def downgrade():
    # Downgrade intentionally minimal — these columns are required by live models.
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at")
    op.execute("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify")
