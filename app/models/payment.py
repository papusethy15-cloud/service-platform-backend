from sqlalchemy import Column, String, DateTime, Text, Float, ForeignKey, Enum as SAEnum, Boolean
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class PaymentMethod(str, enum.Enum):
    RAZORPAY = "RAZORPAY"
    UPI = "UPI"
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    WALLET = "WALLET"
    # Customer payment deferred to a later date/time (due_collect_at on the
    # transaction). Replaces the old reference_number=='PAY_LATER' sentinel hack.
    PAY_LATER = "PAY_LATER"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    # Used to void stale PAY_LATER pending records when a real payment is collected.
    CANCELLED = "CANCELLED"


class CashCollectionStatus(str, enum.Enum):
    PENDING = "PENDING"      # Cash collected by technician, not yet handed to admin/CCO
    COLLECTED = "COLLECTED"  # Admin/CCO has received the cash from technician


class PaymentTransaction(BaseModel):
    __tablename__ = "payment_transactions"

    transaction_number = Column(String(30), unique=True, nullable=False)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    verified_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    method = Column(SAEnum(PaymentMethod), nullable=False)
    status = Column(SAEnum(PaymentStatus), default=PaymentStatus.PENDING)
    amount = Column(Float, default=0.0)
    currency = Column(String(10), default="INR")
    provider_order_id = Column(String(100), nullable=True)
    provider_payment_id = Column(String(100), nullable=True)
    provider_signature = Column(String(255), nullable=True)
    reference_number = Column(String(100), nullable=True)
    payment_link = Column(String(500), nullable=True)
    qr_payload = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    paid_at = Column(DateTime, nullable=True)

    # Cash collection tracking
    # Role of the user who collected the cash (TECHNICIAN / CCO / ADMIN)
    collected_by_role = Column(String(30), nullable=True)
    # If technician collected cash — points to CashCollectionRecord once admin acknowledges
    cash_collection_status = Column(
        SAEnum(CashCollectionStatus),
        nullable=True,
        default=None,
    )

    # ── Pay Later ────────────────────────────────────────────────────────────
    # Date/time the customer promised to pay by. Reminder sweep (see
    # app/main.py) notifies CCO/admin/technician once this is reached.
    due_collect_at   = Column(DateTime(timezone=True), nullable=True)
    # Last time a "please collect payment" reminder was sent for this
    # transaction. Null until the first reminder fires; re-reminds every
    # 24h thereafter while still PENDING.
    last_reminder_at = Column(DateTime(timezone=True), nullable=True)


class CashCollectionRecord(BaseModel):
    """
    Created when a technician collects cash from a customer.
    Tracks that the cash still needs to be handed over to admin/CCO.
    One record per cash PaymentTransaction collected by a technician.
    """
    __tablename__ = "cash_collection_records"

    payment_transaction_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id"), nullable=False, unique=True
    )
    booking_id  = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    invoice_id  = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    customer_id   = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)

    amount = Column(Float, nullable=False)
    status = Column(SAEnum(CashCollectionStatus), default=CashCollectionStatus.PENDING, nullable=False)

    # When admin/CCO marks it as collected
    collected_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    collected_at = Column(DateTime, nullable=True)
    notes        = Column(Text, nullable=True)
