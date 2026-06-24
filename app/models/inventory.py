"""
Inventory Module Models
=======================
Covers: inventory items, categories, brands, units, warehouses,
        warehouse stock, technician stock, stock movements (ledger),
        reorder rules, purchase-linked entries.
"""
import uuid
import enum
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text,
    ForeignKey, Enum as SAEnum, DateTime, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.models.base import Base


# ── Enums ──────────────────────────────────────────────────────────────────

class MovementType(str, enum.Enum):
    PURCHASE      = "PURCHASE"       # stock came in from a vendor / purchase order
    TRANSFER_IN   = "TRANSFER_IN"    # received from another warehouse / tech
    TRANSFER_OUT  = "TRANSFER_OUT"   # sent to another warehouse / tech
    ADJUSTMENT    = "ADJUSTMENT"     # admin manual correction (+/-)
    DAMAGE        = "DAMAGE"         # damaged / written off (-)
    RETURN_IN     = "RETURN_IN"      # returned by technician to warehouse (+)
    RETURN_OUT    = "RETURN_OUT"     # returned to vendor (-)
    ASSIGNMENT    = "ASSIGNMENT"     # dispatched to technician (-)
    CONSUMPTION   = "CONSUMPTION"    # used during repair booking (-)
    SCRAP         = "SCRAP"          # scrapped (-)
    OPENING       = "OPENING"        # initial stock entry (+)

class StockLocationKind(str, enum.Enum):
    WAREHOUSE  = "WAREHOUSE"
    TECHNICIAN = "TECHNICIAN"

class TechnicianStockStatus(str, enum.Enum):
    ASSIGNED   = "ASSIGNED"
    CONSUMED   = "CONSUMED"
    RETURNED   = "RETURNED"
    LOST       = "LOST"
    DAMAGED    = "DAMAGED"


# ── Master / Catalogue ─────────────────────────────────────────────────────

class InventoryCategory(Base):
    __tablename__ = "inventory_categories"
    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name        = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    icon        = Column(String(10))          # emoji
    sort_order  = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    items = relationship("InventoryItem", back_populates="category", lazy="select")


class InventoryBrand(Base):
    __tablename__ = "inventory_brands"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name       = Column(String(100), nullable=False, unique=True)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InventoryItem(Base):
    """
    Master catalogue of spare parts / consumables.
    current_stock is the central warehouse total — technician stock is tracked separately.
    """
    __tablename__ = "inventory_items"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name            = Column(String(200), nullable=False)
    sku             = Column(String(100), unique=True, index=True)
    barcode         = Column(String(100), unique=True, index=True)
    category_id     = Column(UUID(as_uuid=True), ForeignKey("inventory_categories.id"), index=True)
    brand_id        = Column(UUID(as_uuid=True), ForeignKey("appliance_brands.id"), nullable=True)
    unit            = Column(String(20), default="pcs")   # pcs, kg, ltr, m, set …
    description     = Column(Text)
    image_url       = Column(String(500))
    hsn_code        = Column(String(20))       # for GST
    gst_percent     = Column(Float, default=18.0)

    cost_price      = Column(Float, default=0.0)    # purchase / landed cost
    selling_price   = Column(Float, default=0.0)    # price billed to customer
    mrp             = Column(Float, default=0.0)

    # Stock levels (central view)
    current_stock     = Column(Integer, default=0)
    reserved_stock    = Column(Integer, default=0)  # assigned to techs, not yet consumed
    min_stock_level   = Column(Integer, default=0)  # reorder trigger
    reorder_qty       = Column(Integer, default=0)  # suggested PO qty

    is_active         = Column(Boolean, default=True)
    is_consumable     = Column(Boolean, default=False)   # True = lubricants, tapes, etc.
    is_serialised     = Column(Boolean, default=False)   # True = track by serial number
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), onupdate=func.now())

    category = relationship("InventoryCategory", back_populates="items")


# ── Warehouse ──────────────────────────────────────────────────────────────

class Warehouse(Base):
    __tablename__ = "warehouses"
    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name        = Column(String(200), nullable=False)
    code        = Column(String(20), unique=True)
    address     = Column(Text)
    city_id     = Column(UUID(as_uuid=True), ForeignKey("cities.id"))
    city        = Column(String(100))          # denormalised for speed
    manager_id  = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    phone       = Column(String(20))
    is_active   = Column(Boolean, default=True)
    is_default  = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    stock = relationship("WarehouseStock", back_populates="warehouse", lazy="select")


class WarehouseStock(Base):
    """Per-warehouse quantity for each item."""
    __tablename__ = "warehouse_stock"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "item_id", name="uq_wh_item"),
        Index("ix_wh_stock_item", "item_id"),
    )
    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False, index=True)
    item_id      = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False)
    quantity     = Column(Integer, default=0)
    reserved_qty = Column(Integer, default=0)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    warehouse = relationship("Warehouse", back_populates="stock")


# ── Technician Stock ────────────────────────────────────────────────────────

class TechnicianStock(Base):
    """
    Running stock carried by a field technician.
    Populated when admin assigns parts; decremented on consumption.
    """
    __tablename__ = "technician_stock"
    __table_args__ = (
        UniqueConstraint("technician_id", "item_id", name="uq_tech_item"),
        Index("ix_tech_stock_tech", "technician_id"),
    )
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id  = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    item_id        = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False)
    quantity       = Column(Integer, default=0)    # currently in possession
    assigned_qty   = Column(Integer, default=0)    # total ever assigned
    consumed_qty   = Column(Integer, default=0)    # total used in jobs
    returned_qty   = Column(Integer, default=0)    # returned to warehouse
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TechnicianStockLog(Base):
    """
    Every assign / consume / return / damage event for a technician's parts.
    """
    __tablename__ = "technician_stock_logs"
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id  = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False, index=True)
    item_id        = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False, index=True)
    booking_id     = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), index=True)   # nullable — not all events linked to booking
    warehouse_id   = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    status         = Column(String(30), nullable=False)   # DB is VARCHAR
    quantity       = Column(Integer, nullable=False)
    notes          = Column(Text)
    performed_by   = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


# ── Universal Stock Movement Ledger ────────────────────────────────────────

class StockMovement(Base):
    """
    Double-entry-style ledger row for every stock event across the system.
    Positive quantity = stock came IN. Negative = stock went OUT.
    """
    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("ix_mv_item", "item_id"),
        Index("ix_mv_booking", "booking_id"),
        Index("ix_mv_technician", "technician_id"),
        Index("ix_mv_created", "created_at"),
    )

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id         = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False)
    movement_type   = Column(String(30), nullable=False)   # DB is VARCHAR — use .value when writing
    quantity        = Column(Integer, nullable=False)          # +/- signed

    # Context pointers (all optional)
    from_warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    to_warehouse_id   = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    technician_id     = Column(UUID(as_uuid=True), ForeignKey("technicians.id"))
    booking_id        = Column(UUID(as_uuid=True), ForeignKey("bookings.id"))

    reference_no    = Column(String(100))    # PO number, transfer ref, etc.
    batch_no        = Column(String(100))
    reason          = Column(String(300))
    notes           = Column(Text)
    unit_cost       = Column(Float)          # cost at time of movement
    performed_by    = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


# ── Reorder Rules ──────────────────────────────────────────────────────────

class ReorderRule(Base):
    """Auto-alert when stock falls below threshold."""
    __tablename__ = "inventory_reorder_rules"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id         = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False, unique=True)
    warehouse_id    = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    reorder_level   = Column(Integer, nullable=False)
    reorder_qty     = Column(Integer, nullable=False)
    preferred_vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"))
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


# ── Transfer Challan ───────────────────────────────────────────────────────

class TransferChallan(Base):
    """
    Delivery challan generated for every warehouse-to-warehouse
    or warehouse-to-technician stock transfer.
    Provides a printable/trackable document with challan number.
    """
    __tablename__ = "transfer_challans"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    challan_no       = Column(String(30), nullable=False, unique=True, index=True)
    # Transfer direction
    from_warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=True)
    to_warehouse_id   = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=True)
    to_technician_id  = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=True)
    # Items (JSON array: [{item_id, item_name, sku, qty, unit, unit_cost}])
    items_json       = Column(Text, nullable=False, default="[]")
    total_qty        = Column(Integer, default=0)
    total_value      = Column(Float, default=0.0)
    # Status
    status           = Column(String(20), default="PENDING")  # PENDING / IN_TRANSIT / DELIVERED / CANCELLED
    notes            = Column(Text, nullable=True)
    reference_no     = Column(String(100), nullable=True)
    dispatched_at    = Column(DateTime(timezone=True), nullable=True)
    received_at      = Column(DateTime(timezone=True), nullable=True)
    created_by       = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    received_by      = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    is_active        = Column(Boolean, default=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


# ── Direct Sale ────────────────────────────────────────────────────────────

class DirectSale(Base):
    """
    Admin sells a spare part directly to a customer (walk-in / out-of-booking).
    Deducts from warehouse stock, generates an invoice-style record.
    """
    __tablename__ = "direct_sales"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sale_no          = Column(String(30), nullable=False, unique=True, index=True)
    warehouse_id     = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    customer_id      = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    customer_name    = Column(String(200), nullable=True)   # for walk-in without account
    customer_mobile  = Column(String(20), nullable=True)
    booking_id       = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    # Items (JSON array: [{item_id, item_name, sku, qty, unit, unit_price, gst_percent, total}])
    items_json       = Column(Text, nullable=False, default="[]")
    subtotal         = Column(Float, default=0.0)
    gst_amount       = Column(Float, default=0.0)
    total_amount     = Column(Float, default=0.0)
    payment_method   = Column(String(30), default="CASH")   # CASH / UPI / CARD / CREDIT
    payment_status   = Column(String(20), default="PAID")   # PAID / PENDING / PARTIAL
    notes            = Column(Text, nullable=True)
    sold_by          = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    is_active        = Column(Boolean, default=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


# ── Booking Parts Log ──────────────────────────────────────────────────────

class BookingPartUsage(Base):
    """
    Tracks which spare parts were consumed in a specific booking.
    Created when technician marks parts used OR admin records consumption.
    """
    __tablename__ = "booking_part_usage"
    __table_args__ = (Index("ix_bpu_booking", "booking_id"),)

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id      = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    item_id         = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False)
    technician_id   = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=True)
    warehouse_id    = Column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=True)
    quantity        = Column(Integer, nullable=False)
    unit_cost       = Column(Float, default=0.0)
    unit_price      = Column(Float, default=0.0)   # what's charged to customer
    total_amount    = Column(Float, default=0.0)
    notes           = Column(Text, nullable=True)
    created_by      = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
