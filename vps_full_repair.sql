-- ============================================================
-- VPS FULL REPAIR SQL  — idempotent, run any number of times
-- Run as:
--   psql -U bibek_user -d bibek_enterprises -h localhost -p 5432 -W -f /tmp/vps_full_repair.sql
-- ============================================================

-- ── 1. Booking columns (migrations 054/055 may have aborted) ──
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_id       UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_code     VARCHAR(50);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_discount FLOAT DEFAULT 0.0;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city_id         UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pre_cancel_status VARCHAR(30);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pre_reschedule_status VARCHAR(30);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_notes TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_photos TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS inspection_submitted_by VARCHAR(20);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS technician_to_customer_rating FLOAT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS technician_to_customer_notes TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS repeat_of_booking_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS domain_id UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS appliance_brand VARCHAR(100);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS appliance_model VARCHAR(100);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS priority VARCHAR(20) DEFAULT 'NORMAL';

-- ── 2. Payment transaction columns ────────────────────────────
ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS due_collect_at   TIMESTAMP WITH TIME ZONE;
ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS collected_by_role VARCHAR(30);

-- ── 3. Technician columns (053) ───────────────────────────────
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS is_online                BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS fcm_token                VARCHAR(500);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lat                 DOUBLE PRECISION;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_lng                 DOUBLE PRECISION;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_at             TIMESTAMP WITH TIME ZONE;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS auto_assign_eligible     BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS alternate_mobile         VARCHAR(20);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS dob                      DATE;
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS gender                   VARCHAR(10);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS pincode                  VARCHAR(10);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_type            VARCHAR(50);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS identity_number          VARCHAR(50);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_name   VARCHAR(150);
ALTER TABLE technicians ADD COLUMN IF NOT EXISTS emergency_contact_mobile VARCHAR(20);

-- ── 4. User columns ───────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token          VARCHAR(500);
ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid       VARCHAR(128);
ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_url       VARCHAR(500);
ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_url  VARCHAR(500);
ALTER TABLE users ADD COLUMN IF NOT EXISTS id_proof_type      VARCHAR(50);
ALTER TABLE users ADD COLUMN IF NOT EXISTS address_proof_type VARCHAR(50);

-- Unique constraint on firebase_uid (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'uq_users_firebase_uid'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid);
    END IF;
END $$;

-- ── 5. Attendance columns ─────────────────────────────────────
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS accumulated_seconds INTEGER NOT NULL DEFAULT 0;

-- ── 6. Services columns ───────────────────────────────────────
ALTER TABLE services ADD COLUMN IF NOT EXISTS is_pending_verify INTEGER NOT NULL DEFAULT 0;
ALTER TABLE services ADD COLUMN IF NOT EXISTS suggested_by_tech UUID;

-- ── 7. Quotation service items ────────────────────────────────
ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS is_pending_verify        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS custom_service_name      TEXT;
ALTER TABLE quotation_service_items ADD COLUMN IF NOT EXISTS tech_commission_override DOUBLE PRECISION;

-- Make service_id nullable if it isn't already
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name  = 'quotation_service_items'
          AND column_name = 'service_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE quotation_service_items ALTER COLUMN service_id DROP NOT NULL;
    END IF;
END $$;

-- ── 8. Enum values (all idempotent ADD VALUE IF NOT EXISTS) ──
ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'CANCELLED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PENDING_VERIFICATION';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'TECHNICIAN_ACCEPTED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'INVOICE_GENERATED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAYMENT_PENDING';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_STARTED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_PAUSED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'REFUND_INITIATED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAID';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CLOSED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'SETTLED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'QUOTATION_APPROVED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CANCELLATION_REQUESTED';

-- ── 9. Stamp alembic_version to 055 (loop-breaker) ───────────
DELETE FROM alembic_version;
INSERT INTO alembic_version (version_num) VALUES ('055');

SELECT 'VPS FULL REPAIR COMPLETE' AS status;
