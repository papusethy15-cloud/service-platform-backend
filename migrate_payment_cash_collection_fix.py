"""
Run once: ./venv/Scripts/python.exe migrate_payment_cash_collection_fix.py

Fixes column mismatch between SQLAlchemy PaymentTransaction model and the actual
payment_transactions table — migration 022_cash_collection_records defines these
columns but never got applied (alembic has a broken multi-head history), even
though the cash_collection_records table itself exists. Any payment that goes
through /payments/cash (which always writes collected_by_role and, for non
PAY_LATER, cash_collection_status) fails with a DB error, which the frontend
shows generically as "Payment failed".

Missing columns found:
  payment_transactions: collected_by_role, cash_collection_status
"""
import asyncio
from sqlalchemy import text
from app.core.database import engine

MIGRATIONS = [
    ("cashcollectionstatus enum type", """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'cashcollectionstatus') THEN
                CREATE TYPE cashcollectionstatus AS ENUM ('PENDING', 'COLLECTED');
            END IF;
        END$$;
    """),
    ("payment_transactions.collected_by_role column",
        "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS collected_by_role VARCHAR(30)"),
    ("payment_transactions.cash_collection_status column",
        "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS cash_collection_status cashcollectionstatus"),
]


async def main():
    async with engine.begin() as conn:
        for label, sql in MIGRATIONS:
            print(f"-> {label}")
            await conn.execute(text(sql))
    print("\nDone — payment_transactions now matches the model.")


if __name__ == "__main__":
    asyncio.run(main())
