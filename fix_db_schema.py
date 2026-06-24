"""
fix_db_schema.py
Run once to sync DB schema with SQLAlchemy models.
Usage: cd backend && venv/Scripts/python.exe fix_db_schema.py
"""
import asyncio
import asyncpg

DB_URL = "postgresql://palei_user:palei_pass@localhost:5432/palei_solutions"

SQL_FIXES = """
-- =========================================================
-- appliance_brands: add logo_url
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='appliance_brands' AND column_name='logo_url'
  ) THEN
    ALTER TABLE appliance_brands ADD COLUMN logo_url VARCHAR(500);
  END IF;
END $$;

-- =========================================================
-- appliance_types: add category (string) and brand_id FK
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='appliance_types' AND column_name='category'
  ) THEN
    ALTER TABLE appliance_types ADD COLUMN category VARCHAR(100);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='appliance_types' AND column_name='brand_id'
  ) THEN
    ALTER TABLE appliance_types ADD COLUMN brand_id UUID REFERENCES appliance_brands(id);
  END IF;
END $$;

-- =========================================================
-- customer_appliances: add missing columns
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='category'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN category VARCHAR(100);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='type_id'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN type_id UUID REFERENCES appliance_types(id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='installation_date'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN installation_date TIMESTAMPTZ;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='warranty_expiry'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN warranty_expiry TIMESTAMPTZ;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='status'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN status VARCHAR(30) DEFAULT 'ACTIVE';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='notes'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN notes TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='image_url'
  ) THEN
    ALTER TABLE customer_appliances ADD COLUMN image_url VARCHAR(500);
  END IF;
END $$;

-- Rename appliance_type_id -> type_id if old column exists and new doesn't
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='appliance_type_id'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='type_id'
  ) THEN
    ALTER TABLE customer_appliances RENAME COLUMN appliance_type_id TO type_id;
  END IF;
END $$;

-- Rename warranty_end -> warranty_expiry if old column exists
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='customer_appliances' AND column_name='warranty_end'
  ) THEN
    -- Copy data then drop old column (warranty_expiry already added above)
    UPDATE customer_appliances
    SET warranty_expiry = warranty_end::TIMESTAMPTZ
    WHERE warranty_expiry IS NULL AND warranty_end IS NOT NULL;
    ALTER TABLE customer_appliances DROP COLUMN IF EXISTS warranty_end;
  END IF;
END $$;

-- =========================================================
-- appliance_service_history table
-- =========================================================
CREATE TABLE IF NOT EXISTS appliance_service_history (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  appliance_id   UUID NOT NULL REFERENCES customer_appliances(id),
  booking_id     UUID REFERENCES bookings(id),
  service_date   TIMESTAMPTZ DEFAULT NOW(),
  issue_reported TEXT,
  work_done      TEXT,
  technician_id  UUID REFERENCES technicians(id),
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- =========================================================
-- sla_breaches table
-- =========================================================
CREATE TABLE IF NOT EXISTS sla_breaches (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  booking_id   UUID NOT NULL REFERENCES bookings(id),
  policy_id    UUID REFERENCES sla_policies(id),
  breach_type  VARCHAR(30),
  breached_at  TIMESTAMPTZ DEFAULT NOW(),
  notes        TEXT
);

-- sla_policies: add description, resolution_time_hours
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='sla_policies' AND column_name='description'
  ) THEN
    ALTER TABLE sla_policies ADD COLUMN description TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='sla_policies' AND column_name='resolution_time_hours'
  ) THEN
    ALTER TABLE sla_policies ADD COLUMN resolution_time_hours INTEGER;
    -- copy from resolution_time_minutes if exists
    UPDATE sla_policies SET resolution_time_hours = resolution_time_minutes / 60
    WHERE resolution_time_minutes IS NOT NULL;
  END IF;
END $$;

-- =========================================================
-- wallets: add user_id if missing (already has it based on check)
-- model uses user_id as the owner field; old schema used technician_id
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallets' AND column_name='user_id'
  ) THEN
    ALTER TABLE wallets ADD COLUMN user_id UUID REFERENCES users(id);
  END IF;
END $$;

-- =========================================================
-- wallet_transactions: add missing columns
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='wallet_id'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN wallet_id UUID REFERENCES wallets(id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='transaction_type'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN transaction_type VARCHAR(30);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='balance_after'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN balance_after FLOAT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='reference_id'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN reference_id VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='description'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN description TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='status'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN status VARCHAR(20) DEFAULT 'SUCCESS';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='wallet_transactions' AND column_name='amount'
  ) THEN
    ALTER TABLE wallet_transactions ADD COLUMN amount FLOAT;
  END IF;
END $$;

-- =========================================================
-- inventory_items: add missing columns per model
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='inventory_items' AND column_name='min_stock_level'
  ) THEN
    ALTER TABLE inventory_items ADD COLUMN min_stock_level INTEGER DEFAULT 0;
    -- copy from reorder_level if exists
    UPDATE inventory_items SET min_stock_level = reorder_level WHERE reorder_level IS NOT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='inventory_items' AND column_name='current_stock'
  ) THEN
    ALTER TABLE inventory_items ADD COLUMN current_stock INTEGER DEFAULT 0;
    -- copy from quantity if exists
    UPDATE inventory_items SET current_stock = quantity WHERE quantity IS NOT NULL;
  END IF;
END $$;

-- =========================================================
-- inventory_categories: add description
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='inventory_categories' AND column_name='description'
  ) THEN
    ALTER TABLE inventory_categories ADD COLUMN description TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='inventory_categories' AND column_name='is_active'
  ) THEN
    ALTER TABLE inventory_categories ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
  END IF;
END $$;

-- =========================================================
-- commissions: ensure all columns exist
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='commissions' AND column_name='base_amount'
  ) THEN
    ALTER TABLE commissions ADD COLUMN base_amount FLOAT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='commissions' AND column_name='payout_date'
  ) THEN
    ALTER TABLE commissions ADD COLUMN payout_date TIMESTAMPTZ;
  END IF;
END $$;

-- =========================================================
-- refunds: add missing columns per model
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='refunds' AND column_name='refund_method'
  ) THEN
    ALTER TABLE refunds ADD COLUMN refund_method VARCHAR(30) DEFAULT 'ORIGINAL';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='refunds' AND column_name='processed_by'
  ) THEN
    ALTER TABLE refunds ADD COLUMN processed_by UUID REFERENCES users(id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='refunds' AND column_name='processed_at'
  ) THEN
    ALTER TABLE refunds ADD COLUMN processed_at TIMESTAMPTZ;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='refunds' AND column_name='gateway_refund_id'
  ) THEN
    ALTER TABLE refunds ADD COLUMN gateway_refund_id VARCHAR(200);
  END IF;
END $$;

-- =========================================================
-- franchises: align columns to model
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='owner_user_id'
  ) THEN
    ALTER TABLE franchises ADD COLUMN owner_user_id UUID REFERENCES users(id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='state'
  ) THEN
    ALTER TABLE franchises ADD COLUMN state VARCHAR(100);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='address'
  ) THEN
    ALTER TABLE franchises ADD COLUMN address TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='phone'
  ) THEN
    ALTER TABLE franchises ADD COLUMN phone VARCHAR(20);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='email'
  ) THEN
    ALTER TABLE franchises ADD COLUMN email VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='commission_rate'
  ) THEN
    ALTER TABLE franchises ADD COLUMN commission_rate FLOAT DEFAULT 0.0;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='franchises' AND column_name='city'
  ) THEN
    ALTER TABLE franchises ADD COLUMN city VARCHAR(100);
  END IF;
END $$;

-- =========================================================
-- audit_logs: align column names
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='audit_logs' AND column_name='resource_type'
  ) THEN
    ALTER TABLE audit_logs ADD COLUMN resource_type VARCHAR(100);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='audit_logs' AND column_name='resource_id'
  ) THEN
    ALTER TABLE audit_logs ADD COLUMN resource_id VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='audit_logs' AND column_name='old_data'
  ) THEN
    ALTER TABLE audit_logs ADD COLUMN old_data JSONB;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='audit_logs' AND column_name='new_data'
  ) THEN
    ALTER TABLE audit_logs ADD COLUMN new_data JSONB;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='audit_logs' AND column_name='user_agent'
  ) THEN
    ALTER TABLE audit_logs ADD COLUMN user_agent VARCHAR(500);
  END IF;
END $$;

-- =========================================================
-- notifications: add data column (JSONB)
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='notifications' AND column_name='data'
  ) THEN
    ALTER TABLE notifications ADD COLUMN data JSONB;
  END IF;
END $$;

-- =========================================================
-- coupons: ensure discount_type and discount_value exist
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='coupons' AND column_name='discount_type'
  ) THEN
    ALTER TABLE coupons ADD COLUMN discount_type VARCHAR(20);
    -- copy from coupon_type if exists
    UPDATE coupons SET discount_type = CASE WHEN coupon_type='PERCENT' THEN 'PERCENTAGE' ELSE 'FLAT' END
    WHERE coupon_type IS NOT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='coupons' AND column_name='discount_value'
  ) THEN
    ALTER TABLE coupons ADD COLUMN discount_value FLOAT DEFAULT 0;
    UPDATE coupons SET discount_value = COALESCE(value, 0);
  END IF;
END $$;

-- =========================================================
-- coupon_usages: add user_id and used_at if missing
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='coupon_usages' AND column_name='used_at'
  ) THEN
    ALTER TABLE coupon_usages ADD COLUMN used_at TIMESTAMPTZ DEFAULT NOW();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='coupon_usages' AND column_name='discount_applied'
  ) THEN
    ALTER TABLE coupon_usages ADD COLUMN discount_applied FLOAT;
    UPDATE coupon_usages SET discount_applied = COALESCE(discount_amount, 0);
  END IF;
END $$;

-- =========================================================
-- warehouses: ensure all columns exist
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='warehouses' AND column_name='is_active'
  ) THEN
    ALTER TABLE warehouses ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='warehouses' AND column_name='created_at'
  ) THEN
    ALTER TABLE warehouses ADD COLUMN created_at TIMESTAMPTZ DEFAULT NOW();
  END IF;
END $$;

-- =========================================================
-- stock_movements: ensure performed_by exists
-- =========================================================
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='stock_movements' AND column_name='performed_by'
  ) THEN
    ALTER TABLE stock_movements ADD COLUMN performed_by UUID REFERENCES users(id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='stock_movements' AND column_name='movement_type'
  ) THEN
    ALTER TABLE stock_movements ADD COLUMN movement_type VARCHAR(30);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='stock_movements' AND column_name='reason'
  ) THEN
    ALTER TABLE stock_movements ADD COLUMN reason VARCHAR(200);
  END IF;
END $$;
"""

async def run():
    conn = await asyncpg.connect(DB_URL)
    try:
        # Split on -- === to run each block, but asyncpg handles the full script fine
        await conn.execute(SQL_FIXES)
        print("SUCCESS: All schema fixes applied.")
        
        # Verify the key fix
        rows = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='appliance_brands' ORDER BY ordinal_position
        """)
        cols = [r['column_name'] for r in rows]
        print("appliance_brands columns:", cols)
        assert 'logo_url' in cols, "logo_url STILL MISSING!"
        
        rows2 = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='customer_appliances' ORDER BY ordinal_position
        """)
        cols2 = [r['column_name'] for r in rows2]
        print("customer_appliances columns:", cols2)
        
        rows3 = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='appliance_types' ORDER BY ordinal_position
        """)
        print("appliance_types columns:", [r['column_name'] for r in rows3])
        
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        await conn.close()

asyncio.run(run())
