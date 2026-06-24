"""Inventory multi-category: item_service_categories junction table

Revision ID: 006_inventory_multi_cat
Revises: 005_domain_id_cols
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '006_inventory_multi_cat'
down_revision = '005_domain_id_cols'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    def table_exists(name):
        return bind.execute(sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{name}')"
        )).scalar()

    def col_exists(table, col):
        return bind.execute(sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' AND column_name='{col}')"
        )).scalar()

    # ── item_service_categories (M2M junction) ──────────────
    # Each spare part can belong to many service categories.
    # e.g. "Capacitor 40µF" → AC Service, Washing Machine, Refrigerator
    if not table_exists('item_service_categories'):
        op.create_table(
            'item_service_categories',
            sa.Column('item_id',     postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id', ondelete='CASCADE'),
                      nullable=False),
            sa.Column('category_id', postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('service_categories.id', ondelete='CASCADE'),
                      nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
            sa.PrimaryKeyConstraint('item_id', 'category_id',
                                    name='pk_item_service_categories'),
        )
        op.create_index('ix_isc_item_id',     'item_service_categories', ['item_id'])
        op.create_index('ix_isc_category_id', 'item_service_categories', ['category_id'])
        print("Created item_service_categories table")

    # ── Migrate existing single category_id → junction table ─
    # If inventory_items.category_id exists, copy existing data into the new table
    # so no data is lost during the transition
    if col_exists('inventory_items', 'category_id'):
        bind.execute(sa.text("""
            INSERT INTO item_service_categories (item_id, category_id, created_at)
            SELECT i.id, i.category_id, NOW()
            FROM inventory_items i
            WHERE i.category_id IS NOT NULL
              AND i.is_active = true
              AND NOT EXISTS (
                SELECT 1 FROM item_service_categories isc
                WHERE isc.item_id = i.id AND isc.category_id = i.category_id
              )
        """))
        print("Migrated existing single-category data to item_service_categories")
        # NOTE: We intentionally keep inventory_items.category_id for backward compat
        # The route code will ignore it and use the junction table going forward.

    # ── QuotationPartItem: add inventory_item_id FK if missing ─
    # So quotation parts can reference real inventory items for stock tracking
    if not col_exists('quotation_part_items', 'inventory_item_id'):
        op.add_column('quotation_part_items',
            sa.Column('inventory_item_id',
                      postgresql.UUID(as_uuid=True),
                      sa.ForeignKey('inventory_items.id'),
                      nullable=True))
        print("Added inventory_item_id to quotation_part_items")

    if not col_exists('quotation_part_items', 'is_from_stock'):
        op.add_column('quotation_part_items',
            sa.Column('is_from_stock', sa.Boolean(), server_default='false'))
        print("Added is_from_stock to quotation_part_items")


def downgrade():
    op.drop_table('item_service_categories')
    op.drop_column('quotation_part_items', 'inventory_item_id')
    op.drop_column('quotation_part_items', 'is_from_stock')
