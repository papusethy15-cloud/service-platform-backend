"""054_bookings_missing_columns

ROOT CAUSE of /bookings 500 on VPS (but not local):
  Four columns exist in the Booking ORM model but were NEVER added in any
  Alembic migration. Local DB was created via create_all (has them). VPS DB
  was built from incremental migrations (missing them).

  Missing columns:
    bookings.coupon_id       UUID   (nullable)
    bookings.coupon_code     VARCHAR(50) (nullable)
    bookings.coupon_discount FLOAT  (nullable, default 0.0)
    bookings.city_id         UUID   FK → cities.id (nullable)

  All statements use IF NOT EXISTS / DO $$ guards — safe no-op on any DB
  that already has the columns.

  IMPORTANT: do NOT use op.get_bind() in this migration — it is incompatible
  with asyncpg's run_sync bridge and causes "Aborted!" mid-migration.
  All logic (including conditional checks) uses op.execute(text(...)) only.

Revision ID: 054
Revises: 053
Create Date: 2026-07-08 (IST)
"""
from alembic import op
from sqlalchemy import text

revision = '054'
down_revision = '053'
branch_labels = None
depends_on = None


def upgrade():
    # ── bookings: coupon fields ───────────────────────────────────────────────
    op.execute(text(
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_id       UUID"
    ))
    op.execute(text(
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_code     VARCHAR(50)"
    ))
    op.execute(text(
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_discount FLOAT DEFAULT 0.0"
    ))

    # ── bookings: city_id ─────────────────────────────────────────────────────
    op.execute(text(
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city_id UUID"
    ))

    # NOTE: FK bookings.city_id → cities intentionally skipped.
    # Adding FK inside Alembic transactional DDL causes Aborted! if cities
    # table is missing on VPS. FK enforced at ORM level only.

    print("[054] bookings missing columns added: coupon_id, coupon_code, coupon_discount, city_id")


def downgrade():
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS city_id"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_discount"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_code"))
    op.execute(text("ALTER TABLE bookings DROP COLUMN IF EXISTS coupon_id"))
