"""Add extended fields to technicians table

Revision ID: a1b2c3d4e5f6
Revises: 011_purchase_orders
Create Date: 2026-06-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = 'a1b2c3d4e5f6'
down_revision = '011_purchase_orders'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add extended profile columns to technicians table.
    Uses raw SQL with IF NOT EXISTS so it is safe to re-run."""
    conn = op.get_bind()

    new_columns = [
        # (column_name,  DDL_type_string,                             default)
        ('alternate_mobile',          'VARCHAR(20)',   None),
        ('dob',                       'DATE',          None),
        ('gender',                    'VARCHAR(10)',   None),
        ('pincode',                   'VARCHAR(10)',   None),
        ('identity_type',             'VARCHAR(50)',   None),
        ('identity_number',           'VARCHAR(50)',   None),
        ('emergency_contact_name',    'VARCHAR(150)',  None),
        ('emergency_contact_mobile',  'VARCHAR(20)',   None),
    ]

    for col_name, col_type, default in new_columns:
        # PostgreSQL-safe: only adds if column does not already exist
        default_clause = f" DEFAULT {default}" if default is not None else ""
        conn.execute(sa.text(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'technicians'
                    AND   column_name = '{col_name}'
                ) THEN
                    ALTER TABLE technicians
                    ADD COLUMN {col_name} {col_type}{default_clause};
                END IF;
            END
            $$;
        """))


def downgrade() -> None:
    conn = op.get_bind()
    cols = [
        'alternate_mobile', 'dob', 'gender', 'pincode',
        'identity_type', 'identity_number',
        'emergency_contact_name', 'emergency_contact_mobile',
    ]
    for col in cols:
        conn.execute(sa.text(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'technicians'
                    AND   column_name = '{col}'
                ) THEN
                    ALTER TABLE technicians DROP COLUMN {col};
                END IF;
            END
            $$;
        """))
