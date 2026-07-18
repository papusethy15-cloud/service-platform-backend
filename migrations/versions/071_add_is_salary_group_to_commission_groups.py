"""add is_salary_group and salary structure to commission_groups

Revision ID: 071_add_is_salary_group
Revises: 070_add_is_active_to_quotation_part_items
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = '071_add_is_salary_group'
down_revision = '070'
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import text
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS is_salary_group BOOLEAN NOT NULL DEFAULT false"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS monthly_salary FLOAT"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS petrol_amount FLOAT DEFAULT 0"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS mobile_recharge FLOAT DEFAULT 0"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS bonus_amount FLOAT DEFAULT 0"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS hra_amount FLOAT DEFAULT 0"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS other_allowances FLOAT DEFAULT 0"))
    op.execute(text("ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS salary_notes VARCHAR(500)"))


def downgrade():
    op.drop_column('commission_groups', 'salary_notes')
    op.drop_column('commission_groups', 'other_allowances')
    op.drop_column('commission_groups', 'hra_amount')
    op.drop_column('commission_groups', 'bonus_amount')
    op.drop_column('commission_groups', 'mobile_recharge')
    op.drop_column('commission_groups', 'petrol_amount')
    op.drop_column('commission_groups', 'monthly_salary')
    op.drop_column('commission_groups', 'is_salary_group')
