from sqlalchemy import Column, String, DateTime, Text, Float, Integer, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class QuotationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISED = "REVISED"
    EXPIRED = "EXPIRED"
    CONVERTED_TO_INVOICE = "CONVERTED_TO_INVOICE"


class PartSource(str, enum.Enum):
    OFFICE_STOCK = "OFFICE_STOCK"
    MARKET_PURCHASE = "MARKET_PURCHASE"


class Quotation(BaseModel):
    __tablename__ = "quotations"

    quotation_number = Column(String(30), unique=True, nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    domain_id      = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    original_quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=True)
    version = Column(Integer, default=1)
    status = Column(SAEnum(QuotationStatus), default=QuotationStatus.DRAFT)
    labour_charges = Column(Float, default=0.0)
    service_charges = Column(Float, default=0.0)
    services_total = Column(Float, default=0.0)
    parts_total = Column(Float, default=0.0)
    discount_amount = Column(Float, default=0.0)
    adjustment_amount = Column(Float, default=0.0)
    subtotal_amount = Column(Float, default=0.0)
    tax_percent = Column(Float, default=18.0)
    tax_amount = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    remarks = Column(Text, nullable=True)
    # ── Coupon ────────────────────────────────────────────────────────────────
    coupon_id        = Column(UUID(as_uuid=True), nullable=True)
    coupon_code      = Column(String(50),  nullable=True)
    coupon_discount  = Column(Float,       default=0.0, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    # Tax management: NONE = no tax, B2C = consumer GST, B2B = business GST
    tax_mode = Column(String(10), nullable=False, default='B2C')
    customer_gst_number  = Column(String(20),  nullable=True)
    customer_gst_name    = Column(String(200), nullable=True)
    customer_gst_address = Column(Text,        nullable=True)


class QuotationServiceItem(BaseModel):
    __tablename__ = "quotation_service_items"

    quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=False)
    service_id = Column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=False)
    service_name = Column(String(200), nullable=False)
    quantity = Column(Integer, default=1)
    unit_price = Column(Float, default=0.0)
    total_price = Column(Float, default=0.0)
    appliance_label = Column(String(300), nullable=True)          # NEW: which appliance this service belongs to
    is_repeat_complaint = Column(Boolean, default=False)           # NEW: exclude from invoice total


class QuotationPartItem(BaseModel):
    __tablename__ = "quotation_part_items"

    quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=False)
    part_name = Column(String(200), nullable=False)
    part_source = Column(SAEnum(PartSource), default=PartSource.OFFICE_STOCK)
    quantity = Column(Integer, default=1)
    unit_price = Column(Float, default=0.0)   # sale price to customer
    purchase_price = Column(Float, default=0.0)  # cost / purchase price
    total_price = Column(Float, default=0.0)
    vendor_name = Column(String(200), nullable=True)
    bill_number = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    # Link to inventory item if matched from database
    inventory_item_id = Column(UUID(as_uuid=True), ForeignKey("inventory_items.id"), nullable=True)
    # For new parts added by tech/admin — pending admin verification before cataloguing
    is_pending_verify = Column(Integer, default=0)  # 0=no, 1=pending, 2=verified
    is_repeat_complaint = Column(Boolean, default=False)           # NEW: exclude from invoice total


class QuotationStatusLog(BaseModel):
    __tablename__ = "quotation_status_logs"

    quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=False)
    status = Column(SAEnum(QuotationStatus), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)


class QuotationAppliance(BaseModel):
    """Tracks which appliances are added to a quotation with repeat-complaint info."""
    __tablename__ = "quotation_appliances"

    quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=False)
    appliance_id = Column(UUID(as_uuid=True), ForeignKey("customer_appliances.id"), nullable=True)
    appliance_label = Column(String(300), nullable=False)
    is_repeat_complaint = Column(Boolean, default=False)
    repeat_booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    repeat_confirmed_at = Column(DateTime, nullable=True)
