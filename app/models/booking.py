from sqlalchemy import Column, String, DateTime, Text, Float, Integer, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel

class BookingStatus(str, enum.Enum):
    PENDING      = "PENDING"
    CONFIRMED    = "CONFIRMED"
    ASSIGNED     = "ASSIGNED"
    ACCEPTED     = "ACCEPTED"
    EN_ROUTE     = "EN_ROUTE"
    ARRIVED      = "ARRIVED"
    INSPECTING   = "INSPECTING"
    IN_PROGRESS  = "IN_PROGRESS"
    COMPLETED    = "COMPLETED"
    CANCELLED    = "CANCELLED"
    RESCHEDULED  = "RESCHEDULED"
    NO_SHOW         = "NO_SHOW"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    TECHNICIAN_ACCEPTED  = "TECHNICIAN_ACCEPTED"
    INVOICE_GENERATED    = "INVOICE_GENERATED"
    PAYMENT_PENDING      = "PAYMENT_PENDING"
    WORK_STARTED         = "WORK_STARTED"
    WORK_PAUSED          = "WORK_PAUSED"
    REFUND_INITIATED     = "REFUND_INITIATED"
    PAID                 = "PAID"
    CLOSED               = "CLOSED"
    SETTLED              = "SETTLED"
    QUOTATION_APPROVED   = "QUOTATION_APPROVED"

class BookingSource(str, enum.Enum):
    WEBSITE     = "WEBSITE"
    MOBILE_APP  = "MOBILE_APP"
    CALL_CENTER = "CALL_CENTER"
    WALK_IN     = "WALK_IN"
    FRANCHISE   = "FRANCHISE"

class Booking(BaseModel):
    __tablename__ = "bookings"
    booking_number   = Column(String(30), unique=True, nullable=False)
    customer_id      = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    technician_id    = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=True)

    # Structured FK (used by admin/CCO bookings)
    service_id       = Column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=True)   # nullable for public bookings
    address_id       = Column(UUID(as_uuid=True), ForeignKey("customer_addresses.id"), nullable=True)  # nullable for public bookings

    # Free-text fields (used by public website bookings — admin resolves later)
    service_name     = Column(String(200), nullable=True)   # e.g. "Air Conditioner Repair"
    address_line     = Column(Text, nullable=True)
    city             = Column(String(100), nullable=True)
    pincode          = Column(String(10), nullable=True)
    # ── Coupon ────────────────────────────────────────────────────────────────
    coupon_id        = Column(UUID(as_uuid=True), nullable=True)
    coupon_code      = Column(String(50),  nullable=True)
    coupon_discount  = Column(Float,       default=0.0, nullable=True)

    status           = Column(SAEnum(BookingStatus), default=BookingStatus.PENDING)
    source           = Column(SAEnum(BookingSource), default=BookingSource.WEBSITE)
    scheduled_date   = Column(DateTime, nullable=False)
    scheduled_slot   = Column(String(30), nullable=True)
    notes            = Column(Text, nullable=True)
    appliance_brand  = Column(String(100), nullable=True)
    appliance_model  = Column(String(100), nullable=True)
    base_amount      = Column(Float, default=0.0)
    discount_amount  = Column(Float, default=0.0)
    gst_amount       = Column(Float, default=0.0)
    total_amount     = Column(Float, default=0.0)
    priority         = Column(String(20), default="NORMAL")
    cancelled_reason = Column(Text, nullable=True)
    domain_id        = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=True)

class BookingStatusLog(BaseModel):
    __tablename__ = "booking_status_logs"
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    status     = Column(SAEnum(BookingStatus), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    notes      = Column(Text, nullable=True)
