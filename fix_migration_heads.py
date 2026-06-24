"""
Fix: Alembic multiple-heads / overlaps error.
Uses asyncpg (already installed in your venv).

Run:  python fix_migration_heads.py
"""
import asyncio
import asyncpg

HOST     = "localhost"
PORT     = 5433
DATABASE = "palei_solutions"
USER     = "palei_user"
PASSWORD = "palei_pass"

CORRECT_HEAD = "011_purchase_orders"

async def main():
    print("Connecting to PostgreSQL via asyncpg...")
    try:
        conn = await asyncpg.connect(
            host=HOST, port=PORT, database=DATABASE,
            user=USER, password=PASSWORD
        )
    except Exception as e:
        print(f"\n[ERROR] Could not connect: {e}")
        print("\nFall back to MANUAL fix — run these SQL commands in pgAdmin or psql:")
        print_manual_fix()
        return

    print("\n=== Current alembic_version rows ===")
    rows = await conn.fetch("SELECT version_num FROM alembic_version ORDER BY version_num")
    current = [r["version_num"] for r in rows]
    print(f"  {len(current)} row(s): {current}")

    if len(current) == 1 and current[0] == CORRECT_HEAD:
        print(f"\n[✓] DB is already at '{CORRECT_HEAD}' — single row, no overlap.")
        print("    Run:  alembic upgrade heads")
    elif len(current) == 0:
        print(f"\n[!] No version rows found.")
        ans = input(f"    Stamp DB to '{CORRECT_HEAD}'? [yes/no]: ").strip().lower()
        if ans == "yes":
            await conn.execute(
                "INSERT INTO alembic_version (version_num) VALUES ($1)", CORRECT_HEAD
            )
            print(f"[✓] Stamped to '{CORRECT_HEAD}'")
            print("    Run:  alembic upgrade heads")
    else:
        print(f"\n[!] Multiple or wrong rows — this causes the 'overlaps' error.")
        print(f"    Will DELETE all rows and INSERT single correct head: '{CORRECT_HEAD}'")
        print(f"\n    WARNING: This does NOT change any actual database tables.")
        print(f"    It only fixes Alembic's bookkeeping. Safe to do.\n")
        ans = input("    Proceed? [yes/no]: ").strip().lower()
        if ans == "yes":
            async with conn.transaction():
                await conn.execute("DELETE FROM alembic_version")
                await conn.execute(
                    "INSERT INTO alembic_version (version_num) VALUES ($1)", CORRECT_HEAD
                )
            verify = await conn.fetch("SELECT version_num FROM alembic_version")
            print(f"\n[✓] Done. DB now has: {[r['version_num'] for r in verify]}")
            print("    Now run:  alembic upgrade heads")
        else:
            print("\nAborted. No changes made.")
            print_manual_fix()

    await conn.close()

def print_manual_fix():
    print("""
Manual SQL fix (run in pgAdmin Query Tool or psql):

    DELETE FROM alembic_version;
    INSERT INTO alembic_version (version_num) VALUES ('011_purchase_orders');

Then in PowerShell:
    alembic upgrade heads
""")

asyncio.run(main())
