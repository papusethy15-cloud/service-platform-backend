"""055_guaranteed_schema_fix

WHY THIS EXISTS:
  Migrations 053 and 054 both called op.get_bind() which is incompatible
  with asyncpg's run_sync bridge — it causes "Aborted!" mid-migration.
  The DDL in both migrations never actually applied on the VPS.

  This migration re-applies ALL of that DDL using only op.execute(text(...))
  and PostgreSQL DO $$ blocks for conditional logic. Every statement is
  IF NOT EXISTS / idempotent — safe no-op if columns already exist.

  env.py: FINAL_MIGRATION → '055', STAMP_AT → '054'

Revision ID: 055
Revises: 054
Create Date: 2026-07-08 (IST)
"""
from alembic import op
from sqlalchemy import text

revision = '055'
down_revision = '054'
branch_labels = None
depends_on = None


def upgrade():
    # ── technicians (from 053) ────────────────────────────────────────────────
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

    # ── users (from 053) ──────────────────────────────────────────────────────
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token          VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid       VARCHAR(128)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url       VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url  VARCHAR(500)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type      VARCHAR(50)"))
    op.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type VARCHAR(50)"))

    # ── services (from 053) ───────────────────────────────────────────────────
    op.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_pending_verify INTEGER NOT NULL DEFAULT 0"))
    op.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS suggested_by_tech UUID"))

    # ── quotation_service_items (from 053) ────────────────────────────────────
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS is_pending_verify        INTEGER NOT NULL DEFAULT 0"))
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS custom_service_name      TEXT"))
    op.execute(text("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS tech_commission_override DOUBLE PRECISION"))

    # ── payment_transactions (from 053) ───────────────────────────────────────
    op.execute(text("ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS due_collect_at   TIMESTAMP WITH TIME ZONE"))
    op.execute(text("ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP WITH TIME ZONE"))

    # ── quotation_service_items.service_id → nullable (from 053) ─────────────
    op.execute(text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name  = 'quotation_service_items'
                  AND column_name = 'service_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE quotation_service_items
                    ALTER COLUMN service_id DROP NOT NULL;
            END IF;
        END $$;
    """))

    # ── users.firebase_uid unique constraint (from 053) ───────────────────────
    op.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'uq_users_firebase_uid'
            ) THEN
                ALTER TABLE users
                    ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid);
            END IF;
        END $$;
    """))

    # ── bookings: coupon + city_id (from 054) ─────────────────────────────────
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_id       UUID"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_code     VARCHAR(50)"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_discount FLOAT DEFAULT 0.0"))
    op.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city_id         UUID"))

    # NOTE: FK bookings.city_id → cities intentionally skipped here.
    # The FK is enforced at ORM level (nullable=True). Adding it via DDL
    # inside Alembic's transactional DDL block causes Aborted! if cities
    # table is missing or has a type mismatch on VPS.

    print("[055] Guaranteed schema fix complete — all missing columns applied.")


def downgrade():
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS city_id"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_discount"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_code"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_id"))
    op.execute(text("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS last_reminder_at"))
    op.execute(text("ALTER TABLE payment_transactions DROP COLUMN IF EXISTS due_collect_at"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name"))
    op.execute(text("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify"))
    op.execute(text("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech"))
    op.execute(text("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify"))
