-- ============================================================
-- palei_vps_fix.sql
-- Run on VPS palei_solutions DB to bring it up to migration 075
-- Safe: all statements use IF NOT EXISTS / DO $$ guards
--
-- Usage on VPS:
--   PGPASSWORD="SrikantaDB1994" psql -h localhost -p 5432 \
--     -U palei_user palei_solutions -f palei_vps_fix.sql
-- ============================================================

\echo '>>> [1/9] Ensuring required ENUMs exist...'

DO $$ BEGIN
  CREATE TYPE bookingstatus AS ENUM (
    'PENDING','CONFIRMED','ASSIGNED','ACCEPTED','EN_ROUTE','ARRIVED',
    'INSPECTING','IN_PROGRESS','COMPLETED','CANCELLED','RESCHEDULED',
    'NO_SHOW','PENDING_VERIFICATION','TECHNICIAN_ACCEPTED',
    'INVOICE_GENERATED','PAYMENT_PENDING','WORK_STARTED','WORK_PAUSED',
    'REFUND_INITIATED','PAID','CLOSED','SETTLED','QUOTATION_APPROVED',
    'CANCELLATION_REQUESTED'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PENDING_VERIFICATION'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'TECHNICIAN_ACCEPTED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'INVOICE_GENERATED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAYMENT_PENDING'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_STARTED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_PAUSED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'REFUND_INITIATED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAID'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CLOSED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'SETTLED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'QUOTATION_APPROVED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CANCELLATION_REQUESTED'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'NO_SHOW'; EXCEPTION WHEN others THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE userrole AS ENUM ('SUPER_ADMIN','ADMIN','CCO','TECHNICIAN','CUSTOMER','ACCOUNTANT','INVENTORY_MANAGER');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'ACCOUNTANT'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'INVENTORY_MANAGER'; EXCEPTION WHEN others THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE technicianstatus AS ENUM ('ACTIVE','INACTIVE','ON_LEAVE','SUSPENDED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE technicianstatus ADD VALUE IF NOT EXISTS 'ON_LEAVE'; EXCEPTION WHEN others THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE technicianstatus ADD VALUE IF NOT EXISTS 'SUSPENDED'; EXCEPTION WHEN others THEN NULL; END $$;

\echo '>>> [2/9] bookings table columns...'
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS service_name VARCHAR(200);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS address_line TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city VARCHAR(100);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pincode VARCHAR(10);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_code VARCHAR(50);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_discount FLOAT DEFAULT 0.0;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS technician_to_customer_rating FLOAT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS technician_to_customer_notes TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_notes TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_photos TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pre_cancel_status VARCHAR(30);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS repeat_of_booking_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_submitted_by VARCHAR(20);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pre_reschedule_status VARCHAR(30);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS appliance_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_rating FLOAT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_review TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_name VARCHAR(120);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_city VARCHAR(80);

DO $$ BEGIN ALTER TABLE bookings ADD CONSTRAINT fk_bookings_city_id FOREIGN KEY (city_id) REFERENCES cities(id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE bookings ADD CONSTRAINT fk_bookings_repeat_of_booking_id FOREIGN KEY (repeat_of_booking_id) REFERENCES bookings(id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE bookings ADD CONSTRAINT fk_bookings_appliance_id FOREIGN KEY (appliance_id) REFERENCES customer_appliances(id) ON DELETE SET NULL; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

\echo '>>> [3/9] technicians table columns...'
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS payout_upi_id VARCHAR(200);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS payout_bank_account VARCHAR(200);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS payout_bank_ifsc VARCHAR(20);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS payout_bank_name VARCHAR(200);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS payout_account_holder VARCHAR(200);

\echo '>>> [4/9] users table columns...'
ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_upi_id VARCHAR(200);
ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_bank_account VARCHAR(100);
ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_bank_ifsc VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_bank_name VARCHAR(100);
ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_account_holder VARCHAR(150);
ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_salary FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS petrol_amount FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mobile_recharge FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_amount FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS hra_amount FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS other_allowances FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS salary_notes TEXT;

\echo '>>> [5/9] coupons table columns...'
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS customer_mobile_numbers TEXT[];
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS service_ids TEXT[];

\echo '>>> [6/9] commission_groups columns...'
ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS is_salary_group BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS monthly_salary FLOAT;

\echo '>>> [7/9] salary_settlements table...'
CREATE TABLE IF NOT EXISTS salary_settlements (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  technician_id    UUID NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
  month            INTEGER NOT NULL,
  year             INTEGER NOT NULL,
  commission_total FLOAT NOT NULL DEFAULT 0,
  deductions       FLOAT NOT NULL DEFAULT 0,
  deduction_notes  VARCHAR(500),
  net_amount       FLOAT NOT NULL DEFAULT 0,
  status           VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  payment_method   VARCHAR(20),
  payment_ref      VARCHAR(200),
  paid_at          TIMESTAMP WITH TIME ZONE,
  paid_by          UUID REFERENCES users(id) ON DELETE SET NULL,
  settlement_notes TEXT,
  created_at       TIMESTAMP WITH TIME ZONE DEFAULT now(),
  UNIQUE (technician_id, month, year)
);

\echo '>>> [8/9] cco_attendance and cco_salary_settlements tables...'
CREATE TABLE IF NOT EXISTS cco_attendance (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date                DATE NOT NULL,
  check_in            TIMESTAMP WITH TIME ZONE,
  check_out           TIMESTAMP WITH TIME ZONE,
  accumulated_seconds INTEGER NOT NULL DEFAULT 0,
  status              VARCHAR(20) NOT NULL DEFAULT 'PRESENT',
  notes               TEXT,
  created_at          TIMESTAMP WITH TIME ZONE DEFAULT now(),
  UNIQUE (user_id, date)
);
CREATE INDEX IF NOT EXISTS ix_cco_attendance_user_id ON cco_attendance(user_id);
CREATE INDEX IF NOT EXISTS ix_cco_attendance_date    ON cco_attendance(date);

CREATE TABLE IF NOT EXISTS cco_salary_settlements (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  month            INTEGER NOT NULL,
  year             INTEGER NOT NULL,
  monthly_salary   FLOAT NOT NULL DEFAULT 0,
  petrol_amount    FLOAT NOT NULL DEFAULT 0,
  mobile_recharge  FLOAT NOT NULL DEFAULT 0,
  bonus_amount     FLOAT NOT NULL DEFAULT 0,
  hra_amount       FLOAT NOT NULL DEFAULT 0,
  other_allowances FLOAT NOT NULL DEFAULT 0,
  deductions       FLOAT NOT NULL DEFAULT 0,
  deduction_notes  VARCHAR(500),
  total_days       INTEGER NOT NULL DEFAULT 0,
  present_days     INTEGER NOT NULL DEFAULT 0,
  total_hours      FLOAT NOT NULL DEFAULT 0,
  gross_salary     FLOAT NOT NULL DEFAULT 0,
  net_salary       FLOAT NOT NULL DEFAULT 0,
  status           VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  payment_method   VARCHAR(20),
  payment_ref      VARCHAR(200),
  paid_at          TIMESTAMP WITH TIME ZONE,
  paid_by          UUID REFERENCES users(id) ON DELETE SET NULL,
  salary_notes     TEXT,
  created_at       TIMESTAMP WITH TIME ZONE DEFAULT now(),
  UNIQUE (user_id, month, year)
);
CREATE INDEX IF NOT EXISTS ix_cco_salary_user_id ON cco_salary_settlements(user_id);

\echo '>>> [9/9] domain_service_overrides columns...'
ALTER TABLE domain_service_overrides ADD COLUMN IF NOT EXISTS includes_json TEXT;
ALTER TABLE domain_service_overrides ADD COLUMN IF NOT EXISTS excludes_json TEXT;
ALTER TABLE domain_service_overrides ADD COLUMN IF NOT EXISTS faqs_json TEXT;

\echo '>>> withdrawal_requests column...'
ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(200);

\echo '>>> Updating alembic_version to 075...'
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM alembic_version) THEN
    UPDATE alembic_version SET version_num = '075';
  ELSE
    INSERT INTO alembic_version (version_num) VALUES ('075');
  END IF;
END $$;

\echo ''
\echo '============================================================'
\echo 'palei_vps_fix.sql completed successfully!'
\echo 'Next step: pm2 restart palei-backend'
\echo '============================================================'
