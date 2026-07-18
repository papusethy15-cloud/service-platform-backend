"""063_vps_missing_updated_at_is_active_sync

Revision ID: 063
Revises: 062
Create Date: 2026-07-10

Adds missing updated_at and is_active columns to tables that have them
in the SQLAlchemy Base model but were absent on the VPS database.
Generated via live schema diff (local models vs VPS PostgreSQL).
"""
from alembic import op
import sqlalchemy as sa

revision = '063'
down_revision = '062'
branch_labels = None
depends_on = None


def upgrade():
    stmts = [
        # appliance_brands
        "ALTER TABLE appliance_brands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # appliance_service_history
        "ALTER TABLE appliance_service_history ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE appliance_service_history ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # appliance_types
        "ALTER TABLE appliance_types ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
        # attendance
        "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # audit_logs
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # booking_part_usage
        "ALTER TABLE booking_part_usage ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # brand_categories
        "ALTER TABLE brand_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE brand_categories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # commission_group_assignments
        "ALTER TABLE commission_group_assignments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
        "ALTER TABLE commission_group_assignments ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE commission_group_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # commission_group_part_rules
        "ALTER TABLE commission_group_part_rules ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE commission_group_part_rules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # commission_group_rules
        "ALTER TABLE commission_group_rules ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE commission_group_rules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # commission_rules
        "ALTER TABLE commission_rules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # commissions
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # direct_sales
        "ALTER TABLE direct_sales ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # franchises
        "ALTER TABLE franchises ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # inventory_brands
        "ALTER TABLE inventory_brands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
        # inventory_categories
        "ALTER TABLE inventory_categories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # inventory_reorder_rules
        "ALTER TABLE inventory_reorder_rules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # leave_requests
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # notification_templates
        "ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # notifications
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # refunds
        "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # sla_breaches
        "ALTER TABLE sla_breaches ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
        "ALTER TABLE sla_breaches ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE sla_breaches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # sla_policies
        "ALTER TABLE sla_policies ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # stock_movements
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # technician_stock
        "ALTER TABLE technician_stock ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
        "ALTER TABLE technician_stock ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        # technician_stock_logs
        "ALTER TABLE technician_stock_logs ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE technician_stock_logs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # transfer_challans
        "ALTER TABLE transfer_challans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # wallet_transactions
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # warehouse_stock
        "ALTER TABLE warehouse_stock ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
        "ALTER TABLE warehouse_stock ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        # warehouses
        "ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE",
        # withdrawal_requests
        "ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    ]

    for stmt in stmts:
        try:
            op.execute(sa.text(stmt))
        except Exception as e:
            print(f"[SKIP] {stmt[:60]}... -> {e}")


def downgrade():
    # These are additive-only; downgrade is a no-op for safety
    pass
