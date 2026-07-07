"""047_vps_missing_columns_hotfix

Emergency hotfix: applies ALL columns that were missing on the VPS because
the numbered migration chain (001-046) was never run there. The VPS DB was
initialised from an earlier snapshot and only had the original auto-generated
migrations (91baaab49547, fc36bebf9204) stamped.

env.py._maybe_stamp_baseline() detects this state and replaces those legacy
revision IDs with '046', so Alembic runs only this migration (047) on upgrade.

ALL statements use IF NOT EXISTS / try-except so this is a completely safe
no-op on any DB that already has the columns (dev machine, CI, etc.).

Missing columns covered:

  technicians table (from migrations 027, 034, a1b2c3d4e5f6):
    - is_online              BOOLEAN NOT NULL DEFAULT FALSE
    - fcm_token              VARCHAR(500)
    - last_lat               DOUBLE PRECISION
    - last_lng               DOUBLE PRECISION
    - last_seen_at           TIMESTAMP WITH TIME ZONE
    - auto_assign_eligible   BOOLEAN NOT NULL DEFAULT TRUE
    - alternate_mobile       VARCHAR(20)
    - dob                    DATE
    - gender                 VARCHAR(10)
    - pincode                VARCHAR(10)
    - identity_type          VARCHAR(50)
    - identity_number        VARCHAR(50)
    - emergency_contact_name VARCHAR(150)
    - emergency_contact_mobile VARCHAR(20)

  users table (from migrations 031, 032, 039):
    - fcm_token              VARCHAR(500)
    - firebase_uid           VARCHAR(128)  [+ unique constraint]
    - id_proof_url           VARCHAR(500)
    - address_proof_url      VARCHAR(500)
    - id_proof_type          VARCHAR(50)
    - address_proof_type     VARCHAR(50)

  services table (from migration 030):
    - is_pending_verify      INTEGER NOT NULL DEFAULT 0
    - suggested_by_tech      UUID

  quotation_service_items table (from migration 030):
    - is_pending_verify          INTEGER NOT NULL DEFAULT 0
    - custom_service_name        TEXT
    - tech_commission_override   FLOAT

Revision ID: 047
Revises: 046
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = '047'
down_revision = '046'
branch_labels = None
depends_on = None


def upgrade():
    # ── technicians table ────────────────────────────────────────────────
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS is_online BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(500)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lat DOUBLE PRECISION")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lng DOUBLE PRECISION")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS auto_assign_eligible BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS alternate_mobile VARCHAR(20)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS dob DATE")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS gender VARCHAR(10)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS pincode VARCHAR(10)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_type VARCHAR(50)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_number VARCHAR(50)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_name VARCHAR(150)")
    op.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_mobile VARCHAR(20)")

    # ── users table ──────────────────────────────────────────────────────
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(500)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid VARCHAR(128)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url VARCHAR(500)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url VARCHAR(500)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type VARCHAR(50)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type VARCHAR(50)")

    # Unique constraint on firebase_uid — skip if already exists
    try:
        op.create_unique_constraint('uq_users_firebase_uid', 'users', ['firebase_uid'])
    except Exception:
        pass

    # ── services table ───────────────────────────────────────────────────
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS is_pending_verify INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS suggested_by_tech UUID")

    # ── quotation_service_items table ────────────────────────────────────
    op.execute("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS is_pending_verify INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS custom_service_name TEXT")
    op.execute("ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS tech_commission_override FLOAT")

    # Make service_id nullable (already idempotent for nullable columns)
    try:
        op.alter_column('quotation_service_items', 'service_id', nullable=True)
    except Exception:
        pass


def downgrade():
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS tech_commission_override")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS custom_service_name")
    op.execute("ALTER TABLE quotation_service_items DROP COLUMN IF EXISTS is_pending_verify")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS suggested_by_tech")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS is_pending_verify")
    try:
        op.drop_constraint('uq_users_firebase_uid', 'users', type_='unique')
    except Exception:
        pass
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
