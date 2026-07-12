#!/bin/bash
# fix_migration_067.sh
# Run this on VPS to insert the rows that migration 067 failed to insert.
# The migration was force-stamped so alembic thinks it ran — this applies the SQL manually.

DB_URL="postgresql://bibek_user:Bibek@2026#Secure@localhost:5432/bibek-enterprises"

psql "$DB_URL" << SQLEOF
INSERT INTO system_settings ("group", key, value, is_secret, label)
VALUES
  ('payment', 'razorpay_payout_enabled',   'false',  false, 'Enable automatic payouts via Razorpay X'),
  ('payment', 'razorpay_x_key_id',         '',       false, 'Razorpay X API Key ID (rzp_live_...)'),
  ('payment', 'razorpay_x_key_secret',     '',       true,  'Razorpay X API Key Secret - stored encrypted'),
  ('payment', 'razorpay_x_account_number', '',       false, 'Razorpay X fund account number'),
  ('payment', 'withdrawal_payout_mode',    'manual', false, 'Payout mode: manual or razorpay')
ON CONFLICT ("group", key) DO NOTHING;

SELECT "group", key, value, is_secret FROM system_settings WHERE "group" = 'payment' ORDER BY key;
SQLEOF
