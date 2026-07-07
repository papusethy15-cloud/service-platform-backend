"""
038_consolidate_adhoc_schema_fixes

Historically this project had a pile of one-off, hand-run scripts
(migrate_*.py / fix_*.py in the backend root) applying raw ALTER TABLE
statements directly against the dev database, from BEFORE the Alembic
auto-migrate-on-startup fix. Those columns already exist on the live dev
DB (each script was run manually), but several were never captured in
the Alembic migration chain — meaning a fresh database (new dev machine,
CI, production) would be missing them.

This migration formalizes every column those scripts added that isn't
already covered by an existing migration, using idempotent
`ADD COLUMN IF NOT EXISTS` (safe no-op on the current dev DB, and makes
a fresh install match it). This lets the old ad-hoc scripts be safely
deleted from the project without any loss of reproducibility.

Source scripts folded in here:
  - migrate_assignment_response_deadline.py  -> assignment_history.response_deadline
  - migrate_attendance_accumulated_seconds.py -> attendance.accumulated_seconds
  - migrate_booking_customer_rating.py       -> bookings.technician_to_customer_rating/_notes
  - fix_all_remaining.py                     -> audit_logs.description, leave_requests.reviewed_at,
                                                 notification_templates.title
                                                 (warehouse_stock.reserved_qty and
                                                 stock_movements.challan_id already covered by
                                                 migration 009, re-applied here too as a no-op safety net)
  - fix_warehouses.py                        -> warehouses.city, warehouses.address
                                                 (already covered by migrations 002/009 in most
                                                 schema histories, re-applied here as a no-op
                                                 safety net since 002's CREATE is conditional)
  - fix_final.py                             -> technician_availability.is_active/updated_at
                                                 (already covered by migration 001, no-op safety net)
  - fix_db_schema.py                         -> remaining appliance/inventory/wallet/franchise/coupon
                                                 columns not already covered by migrations 001/002/006/009
                                                 (see full list below). The RENAME/DROP statements from
                                                 that script are intentionally NOT replayed here — the
                                                 schema has since diverged past them (e.g. customer_appliances
                                                 now has both the old appliance_type_id and the newer
                                                 type_id/appliance_category_id columns from migration 008),
                                                 and blindly replaying a rename against today's schema
                                                 would be destructive rather than corrective.

Revision ID: 038
Revises: 037
"""
from alembic import op
import sqlalchemy as sa

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


# (table, column, DDL type/default clause)
_COLUMNS = [
    ("assignment_history",      "response_deadline",             "TIMESTAMP"),
    ("attendance",              "accumulated_seconds",           "INTEGER NOT NULL DEFAULT 0"),
    ("bookings",                "technician_to_customer_rating",  "FLOAT"),
    ("bookings",                "technician_to_customer_notes",   "TEXT"),
    ("audit_logs",              "description",                   "TEXT"),
    ("audit_logs",              "resource_type",                 "VARCHAR(100)"),
    ("audit_logs",              "resource_id",                   "VARCHAR(200)"),
    ("audit_logs",              "old_data",                      "JSONB"),
    ("audit_logs",              "new_data",                      "JSONB"),
    ("audit_logs",              "user_agent",                    "VARCHAR(500)"),
    ("leave_requests",          "reviewed_at",                   "TIMESTAMP WITH TIME ZONE"),
    ("notification_templates",  "title",                         "VARCHAR(200)"),
    ("warehouse_stock",         "reserved_qty",                  "INTEGER DEFAULT 0"),
    ("stock_movements",         "challan_id",                    "UUID REFERENCES transfer_challans(id)"),
    ("stock_movements",         "performed_by",                  "UUID REFERENCES users(id)"),
    ("stock_movements",         "movement_type",                 "VARCHAR(30)"),
    ("stock_movements",         "reason",                        "VARCHAR(200)"),
    ("warehouses",              "city",                          "VARCHAR(100)"),
    ("warehouses",              "address",                       "TEXT"),
    ("warehouses",              "is_active",                     "BOOLEAN DEFAULT TRUE"),
    ("warehouses",              "created_at",                    "TIMESTAMPTZ DEFAULT NOW()"),
    ("appliance_types",         "category",                      "VARCHAR(100)"),
    ("appliance_types",         "brand_id",                      "UUID REFERENCES appliance_brands(id)"),
    ("customer_appliances",     "status",                        "VARCHAR(30) DEFAULT 'ACTIVE'"),
    ("customer_appliances",     "notes",                         "TEXT"),
    ("customer_appliances",     "image_url",                     "VARCHAR(500)"),
    ("sla_policies",            "description",                   "TEXT"),
    ("sla_policies",            "resolution_time_hours",         "INTEGER"),
    ("wallets",                 "user_id",                       "UUID REFERENCES users(id)"),
    ("wallet_transactions",     "wallet_id",                     "UUID REFERENCES wallets(id)"),
    ("wallet_transactions",     "transaction_type",               "VARCHAR(30)"),
    ("wallet_transactions",     "balance_after",                 "FLOAT"),
    ("wallet_transactions",     "reference_id",                  "VARCHAR(200)"),
    ("wallet_transactions",     "description",                   "TEXT"),
    ("wallet_transactions",     "status",                        "VARCHAR(20) DEFAULT 'SUCCESS'"),
    ("wallet_transactions",     "amount",                        "FLOAT"),
    ("inventory_items",         "min_stock_level",                "INTEGER DEFAULT 0"),
    ("inventory_items",         "current_stock",                 "INTEGER DEFAULT 0"),
    ("inventory_categories",    "description",                   "TEXT"),
    ("inventory_categories",    "is_active",                     "BOOLEAN DEFAULT TRUE"),
    ("commissions",             "base_amount",                   "FLOAT"),
    ("commissions",             "payout_date",                   "TIMESTAMPTZ"),
    ("refunds",                 "refund_method",                 "VARCHAR(30) DEFAULT 'ORIGINAL'"),
    ("refunds",                 "processed_by",                  "UUID REFERENCES users(id)"),
    ("refunds",                 "processed_at",                  "TIMESTAMPTZ"),
    ("refunds",                 "gateway_refund_id",             "VARCHAR(200)"),
    ("franchises",              "owner_user_id",                 "UUID REFERENCES users(id)"),
    ("franchises",              "state",                         "VARCHAR(100)"),
    ("franchises",              "address",                       "TEXT"),
    ("franchises",              "phone",                         "VARCHAR(20)"),
    ("franchises",              "email",                         "VARCHAR(200)"),
    ("franchises",              "commission_rate",                "FLOAT DEFAULT 0.0"),
    ("franchises",              "city",                          "VARCHAR(100)"),
    ("notifications",           "data",                          "JSONB"),
    ("coupons",                 "discount_type",                 "VARCHAR(20)"),
    ("coupons",                 "discount_value",                "FLOAT DEFAULT 0"),
    ("coupon_usages",           "used_at",                       "TIMESTAMPTZ DEFAULT NOW()"),
    ("coupon_usages",           "discount_applied",              "FLOAT"),
    ("technician_availability", "is_active",                     "BOOLEAN DEFAULT TRUE"),
    ("technician_availability", "updated_at",                    "TIMESTAMP WITH TIME ZONE"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, ddl in _COLUMNS:
        # All target tables are created by earlier migrations (001-013), so
        # a plain ADD COLUMN IF NOT EXISTS is safe and idempotent — no need
        # for per-statement savepoints.
        conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))



def downgrade() -> None:
    # Additive-only, idempotent safety-net migration — no downgrade provided
    # (columns may be relied on by later migrations/models).
    pass
