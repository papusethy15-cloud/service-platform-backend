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
    # Customer/technician requested cancellation before technician arrival —
    # awaiting admin/CCO to confirm (→ CANCELLED) or reject (→ restored to
    # pre_cancel_status). Admin/CCO-initiated cancellations skip this and go
    # straight to CANCELLED since they already carry that authority.
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"

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
    # Status the booking was in right before a CANCELLATION_REQUESTED was raised,
    # so admin/CCO rejecting the request can restore it exactly. Cleared once
    # the request is resolved (confirmed or rejected).
    pre_cancel_status = Column(String(30), nullable=True)
    # Status the booking was in RIGHT BEFORE it was set to RESCHEDULED,
    # so the technician/CCO/admin can resume at the correct repair stage
    # after the rescheduled visit happens. Examples: INSPECTING, IN_PROGRESS.
    # Cleared (set to None) when the booking advances past RESCHEDULED.
    pre_reschedule_status = Column(String(30), nullable=True)
    domain_id        = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=True)
    # Set when this booking was created via customer "report an issue" within
    # 10 days of the original booking's closure (repeat-complaint flow). Used
    # at settlement time to resolve the technician who did the original job.
    repeat_of_booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    city_id          = Column(UUID(as_uuid=True), ForeignKey("cities.id"),  nullable=True)
    # Inspection data — saved when technician OR CCO submits inspection (INSPECTING → IN_PROGRESS)
    inspection_notes         = Column(Text, nullable=True)
    inspection_photos        = Column(Text, nullable=True)  # JSON array of Cloudinary URLs
    inspection_submitted_by  = Column(String(20), nullable=True)  # 'TECHNICIAN' | 'CCO' | 'ADMIN'

    # Technician rates the customer after job completion
    technician_to_customer_rating = Column(Float, nullable=True)
    technician_to_customer_notes  = Column(Text, nullable=True)

class BookingStatusLog(BaseModel):
    __tablename__ = "booking_status_logs"
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    status     = Column(SAEnum(BookingStatus), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    notes      = Column(Text, nullable=True)
