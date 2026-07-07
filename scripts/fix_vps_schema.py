#!/usr/bin/env python3
"""
fix_vps_schema.py
=================
Standalone VPS schema fix script. Run this ONCE on the VPS to add all
columns that were missing after bootstrapping from an old DB snapshot.

Usage (on VPS):
    cd /opt/backend/bibekenterprises-backend
    source venv/bin/activate
    python scripts/fix_vps_schema.py

What it does:
  1. Connects using the same DATABASE_URL as the app (.env is loaded)
  2. Adds missing columns to: technicians, users, services,
     quotation_service_items, payment_transactions
  3. Adds PAY_LATER / CANCELLED enum values (run OUTSIDE transaction —
     PostgreSQL requires this for ALTER TYPE ADD VALUE)
  4. Stamps alembic_version to '049' so future `alembic upgrade head`
     works normally without re-running these fixes
  5. All operations are idempotent (IF NOT EXISTS / try-except)

Safe to run on any DB — no-ops if columns already exist.
"""

import os, sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ── Load .env so DATABASE_URL is populated ───────────────────────────────────
from pathlib import Path
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

raw_url = os.environ.get("DATABASE_URL", "")
if not raw_url:
    # fallback default (dev)
    raw_url = "postgresql://palei_user:palei_pass@localhost:5433/palei_solutions"

# Strip asyncpg prefix if present
url = raw_url.replace("postgresql+asyncpg://", "postgresql://")
print(f"[fix_vps_schema] Connecting to: {url[:40]}...")

conn = psycopg2.connect(url)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: ENUM VALUE ADDITIONS  (must run in AUTOCOMMIT — no transaction)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1/3] Adding enum values (AUTOCOMMIT mode)...")
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()

enum_ops = [
    ("paymentmethod", "PAY_LATER"),
    ("paymentstatus", "CANCELLED"),
]

for enum_type, value in enum_ops:
    try:
        # Check if value already exists
        cur.execute("""
            SELECT 1 FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = %s AND e.enumlabel = %s
        """, (enum_type, value))
        if cur.fetchone():
            print(f"  SKIP   {enum_type}.{value} (already exists)")
        else:
            cur.execute(f"ALTER TYPE {enum_type} ADD VALUE '{value}'")
            print(f"  ADDED  {enum_type}.{value}")
    except Exception as e:
        print(f"  ERROR  {enum_type}.{value}: {e}")

cur.close()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: COLUMN ADDITIONS  (run in normal transaction)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2/3] Adding missing columns (transactional)...")
conn.set_isolation_level(0)  # back to default (READ COMMITTED)
conn.autocommit = False
cur = conn.cursor()

# Each entry: (table, column, sql_type)
columns = [
    # technicians
    ("technicians", "is_online",               "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("technicians", "fcm_token",               "VARCHAR(500)"),
    ("technicians", "last_lat",                "DOUBLE PRECISION"),
    ("technicians", "last_lng",                "DOUBLE PRECISION"),
    ("technicians", "last_seen_at",            "TIMESTAMP WITH TIME ZONE"),
    ("technicians", "auto_assign_eligible",    "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("technicians", "alternate_mobile",        "VARCHAR(20)"),
    ("technicians", "dob",                     "DATE"),
    ("technicians", "gender",                  "VARCHAR(10)"),
    ("technicians", "pincode",                 "VARCHAR(10)"),
    ("technicians", "identity_type",           "VARCHAR(50)"),
    ("technicians", "identity_number",         "VARCHAR(50)"),
    ("technicians", "emergency_contact_name",  "VARCHAR(150)"),
    ("technicians", "emergency_contact_mobile","VARCHAR(20)"),

    # users
    ("users", "fcm_token",         "VARCHAR(500)"),
    ("users", "firebase_uid",      "VARCHAR(128)"),
    ("users", "id_proof_url",      "VARCHAR(500)"),
    ("users", "address_proof_url", "VARCHAR(500)"),
    ("users", "id_proof_type",     "VARCHAR(50)"),
    ("users", "address_proof_type","VARCHAR(50)"),

    # services
    ("services", "is_pending_verify", "INTEGER NOT NULL DEFAULT 0"),
    ("services", "suggested_by_tech", "UUID"),

    # quotation_service_items
    ("quotation_service_items", "is_pending_verify",       "INTEGER NOT NULL DEFAULT 0"),
    ("quotation_service_items", "custom_service_name",     "TEXT"),
    ("quotation_service_items", "tech_commission_override","DOUBLE PRECISION"),

    # payment_transactions
    ("payment_transactions", "due_collect_at",   "TIMESTAMP WITH TIME ZONE"),
    ("payment_transactions", "last_reminder_at", "TIMESTAMP WITH TIME ZONE"),
]

for table, col, coltype in columns:
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name=%s AND column_name=%s
        """, (table, col))
        if cur.fetchone():
            print(f"  SKIP   {table}.{col} (exists)")
        else:
            cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {coltype}')
            print(f"  ADDED  {table}.{col} {coltype}")
    except Exception as e:
        conn.rollback()
        print(f"  ERROR  {table}.{col}: {e}")
        continue

# Make service_id nullable on quotation_service_items
try:
    cur.execute("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name='quotation_service_items' AND column_name='service_id'
    """)
    row = cur.fetchone()
    if row and row[0] == 'NO':
        cur.execute('ALTER TABLE quotation_service_items ALTER COLUMN service_id DROP NOT NULL')
        print("  FIXED  quotation_service_items.service_id -> nullable")
    else:
        print("  SKIP   quotation_service_items.service_id (already nullable)")
except Exception as e:
    conn.rollback()
    print(f"  ERROR  service_id nullable: {e}")

# Unique constraint on users.firebase_uid
try:
    cur.execute("""
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name='uq_users_firebase_uid'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE users ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid)")
        print("  ADDED  constraint uq_users_firebase_uid")
    else:
        print("  SKIP   constraint uq_users_firebase_uid (exists)")
except Exception as e:
    conn.rollback()
    print(f"  WARN   uq_users_firebase_uid: {e}")

conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: STAMP ALEMBIC TO 049
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3/3] Stamping alembic_version to 049...")
try:
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name='alembic_version'
        )
    """)
    has_table = cur.fetchone()[0]

    if not has_table:
        cur.execute("""
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
        """)

    cur.execute("DELETE FROM alembic_version")
    cur.execute("INSERT INTO alembic_version (version_num) VALUES ('049')")
    conn.commit()
    print("  DONE   alembic_version = 049")
except Exception as e:
    conn.rollback()
    print(f"  ERROR  stamping: {e}")

cur.close()
conn.close()
print("\n✅  fix_vps_schema.py complete. Run: pm2 restart bibek-backend")
