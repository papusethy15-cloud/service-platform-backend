"""VPS missing columns fix: inventory_items.gst_percent, vendors full column set

Revision ID: 076
Revises: 075
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = '076'
down_revision = '075'
branch_labels = None
depends_on = None


def _col_exists(conn, table, column):
    row = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).fetchone()
    return row is not None


def upgrade():
    conn = op.get_context().connection

    # ── Fix 1: inventory_items.gst_percent ─────────────────────────────────
    if not _col_exists(conn, "inventory_items", "gst_percent"):
        conn.execute(sa.text(
            "ALTER TABLE inventory_items ADD COLUMN gst_percent FLOAT DEFAULT 18.0"
        ))
        print("[OK] Added inventory_items.gst_percent")
    else:
        print("[SKIP] inventory_items.gst_percent already exists")

    # ── Fix 2: vendors — add ALL columns the ORM model expects ─────────────
    vendor_cols = [
        ("contact_person", "VARCHAR(150)"),
        ("mobile",         "VARCHAR(20)"),
        ("email",          "VARCHAR(200)"),
        ("gstin",          "VARCHAR(20)"),
        ("address",        "TEXT"),
    ]
    for col_name, col_type in vendor_cols:
        if not _col_exists(conn, "vendors", col_name):
            conn.execute(sa.text(
                f"ALTER TABLE vendors ADD COLUMN {col_name} {col_type}"
            ))
            print(f"[OK] Added vendors.{col_name}")
        else:
            print(f"[SKIP] vendors.{col_name} already exists")


def downgrade():
    conn = op.get_context().connection
    conn.execute(sa.text("ALTER TABLE inventory_items DROP COLUMN IF EXISTS gst_percent"))
    for col in ("contact_person", "mobile", "email", "gstin", "address"):
        conn.execute(sa.text(f"ALTER TABLE vendors DROP COLUMN IF EXISTS {col}"))
