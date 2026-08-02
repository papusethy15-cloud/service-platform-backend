"""Fix wallets table for customer wallet support

The original wallets migration (002) created the table with:
  - technician_id NOT NULL (blocks customer wallets which have no technician)
  - No user_id column at all
  - wallet_transactions had 'notes' column instead of 'description'
  - wallet_transactions had 'description' missing

This migration fixes all of these idempotently.

Revision ID: 078
Revises: 077
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = '078'
down_revision = '077'
branch_labels = None
depends_on = None


def _col_exists(conn, table, column):
    row = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).fetchone()
    return row is not None


def _table_exists(conn, table):
    row = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table}).fetchone()
    return row is not None


def upgrade():
    conn = op.get_context().connection

    if not _table_exists(conn, "wallets"):
        print("[SKIP] wallets table does not exist — skipping wallet fixes")
        return

    # ── Fix 1: Add user_id column to wallets (for customer wallets) ─────────
    if not _col_exists(conn, "wallets", "user_id"):
        conn.execute(sa.text(
            "ALTER TABLE wallets ADD COLUMN user_id UUID REFERENCES users(id)"
        ))
        print("[OK] Added wallets.user_id")
    else:
        print("[SKIP] wallets.user_id already exists")

    # ── Fix 2: Make technician_id nullable (customers have no technician) ────
    # Check if the column is currently NOT NULL
    nullable_check = conn.execute(sa.text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='wallets' AND column_name='technician_id'"
    )).fetchone()
    if nullable_check and nullable_check[0] == 'NO':
        conn.execute(sa.text(
            "ALTER TABLE wallets ALTER COLUMN technician_id DROP NOT NULL"
        ))
        print("[OK] Made wallets.technician_id nullable")
    else:
        print("[SKIP] wallets.technician_id already nullable")

    # ── Fix 3: Add pending_amount if missing (some installations skip it) ────
    if not _col_exists(conn, "wallets", "pending_amount"):
        conn.execute(sa.text(
            "ALTER TABLE wallets ADD COLUMN pending_amount FLOAT DEFAULT 0.0"
        ))
        print("[OK] Added wallets.pending_amount")
    else:
        print("[SKIP] wallets.pending_amount already exists")

    # ── Fix 4: wallet_transactions — add 'description' column ────────────────
    if _table_exists(conn, "wallet_transactions"):
        if not _col_exists(conn, "wallet_transactions", "description"):
            conn.execute(sa.text(
                "ALTER TABLE wallet_transactions ADD COLUMN description TEXT"
            ))
            print("[OK] Added wallet_transactions.description")
        else:
            print("[SKIP] wallet_transactions.description already exists")

        # Fix 5: wallet_transactions reference_id may be UUID type but ORM uses String
        # If reference_id is UUID type, change to VARCHAR(200)
        ref_type = conn.execute(sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='wallet_transactions' AND column_name='reference_id'"
        )).fetchone()
        if ref_type and ref_type[0] == 'uuid':
            conn.execute(sa.text(
                "ALTER TABLE wallet_transactions "
                "ALTER COLUMN reference_id TYPE VARCHAR(200) USING reference_id::VARCHAR"
            ))
            print("[OK] Changed wallet_transactions.reference_id from UUID to VARCHAR(200)")
        else:
            print("[SKIP] wallet_transactions.reference_id type already correct")

        # Fix 6: Add balance_before if missing
        if not _col_exists(conn, "wallet_transactions", "balance_before"):
            conn.execute(sa.text(
                "ALTER TABLE wallet_transactions ADD COLUMN balance_before FLOAT"
            ))
            print("[OK] Added wallet_transactions.balance_before")
        else:
            print("[SKIP] wallet_transactions.balance_before already exists")
    else:
        print("[SKIP] wallet_transactions table does not exist")

    print("[OK] Migration 078 complete")


def downgrade():
    conn = op.get_context().connection
    # Only drop what we added — don't remove user_id if data exists
    # (downgrade is rarely used in production; just document the intent)
    print("[INFO] Downgrade 078: no-op (manual cleanup required if needed)")
