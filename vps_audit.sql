-- ============================================================
-- VPS AUDIT SQL — run this to find exactly what's missing
-- Run as:
--   psql -U bibek_user -d bibek_enterprises -h localhost -p 5432 -W -f /tmp/vps_audit.sql
-- ============================================================

-- 1. Which critical tables exist?
SELECT 'TABLE_EXISTS' as check_type, table_name
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name IN (
    'users','bookings','customers','customer_addresses',
    'technicians','services','service_categories',
    'domains','domain_profiles','domain_cities',
    'wallets','wallet_transactions','withdrawal_requests',
    'commissions','commission_rules',
    'invoices','payment_transactions','cash_collection_records',
    'quotations','quotation_service_items','quotation_part_items',
    'assignment_history','assignment_rules',
    'attendance','leave_requests',
    'callback_requests','cities',
    'inventory_items','item_service_categories',
    'notifications','system_settings',
    'coupons','alembic_version'
  )
ORDER BY table_name;

-- 2. Which critical tables are MISSING?
SELECT 'TABLE_MISSING' as check_type, t.expected
FROM (VALUES
  ('users'),('bookings'),('customers'),('customer_addresses'),
  ('technicians'),('services'),('service_categories'),
  ('domains'),('domain_profiles'),('domain_cities'),
  ('wallets'),('wallet_transactions'),('withdrawal_requests'),
  ('commissions'),('commission_rules'),
  ('invoices'),('payment_transactions'),('cash_collection_records'),
  ('quotations'),('quotation_service_items'),('quotation_part_items'),
  ('assignment_history'),('assignment_rules'),
  ('attendance'),('leave_requests'),
  ('callback_requests'),('cities'),
  ('inventory_items'),('item_service_categories'),
  ('notifications'),('system_settings'),
  ('coupons'),('alembic_version')
) AS t(expected)
WHERE t.expected NOT IN (
  SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'
)
ORDER BY t.expected;

-- 3. Current alembic version
SELECT 'ALEMBIC_VERSION' as check_type, version_num FROM alembic_version;

-- 4. Check specific columns on customers table
SELECT 'customers.' || column_name as present_column
FROM information_schema.columns
WHERE table_name = 'customers'
ORDER BY ordinal_position;

-- 5. Check commissions columns
SELECT 'commissions.' || column_name as present_column
FROM information_schema.columns
WHERE table_name = 'commissions'
ORDER BY ordinal_position;

-- 6. Check callback_requests columns  
SELECT 'callback_requests.' || column_name as present_column
FROM information_schema.columns
WHERE table_name = 'callback_requests'
ORDER BY ordinal_position;

-- 7. Show any CURRENT errors in the database log (last 5 errors from pg_log if accessible)
SELECT now() as audit_time, current_database() as db_name;
