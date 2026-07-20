"""Add remaining missing columns to vendors table

Revision ID: 077
Revises: 076
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = '077'
down_revision = '076'
branch_labels = None
depends_on = None


def _col_exists(conn, table, column):
    row = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).fetchone()
    return row is not None


def upgrade():
    conn = op.get_bind()

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
    conn = op.get_bind()
    for col in ("contact_person", "mobile", "email", "gstin", "address"):
        conn.execute(sa.text(f"ALTER TABLE vendors DROP COLUMN IF EXISTS {col}"))
