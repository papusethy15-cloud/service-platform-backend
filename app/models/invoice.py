from sqlalchemy import Column, String, DateTime, Text, Float, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class InvoiceType(str, enum.Enum):
    GST_B2C = "GST_B2C"
    GST_B2B = "GST_B2B"
    NON_GST = "NON_GST"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    GENERATED = "GENERATED"
    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class Invoice(BaseModel):
    __tablename__ = "invoices"

    invoice_number = Column(String(30), unique=True, nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    domain_id      = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=True)
    quotation_id = Column(UUID(as_uuid=True), ForeignKey("quotations.id"), nullable=False, unique=True)
    generated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    invoice_type = Column(SAEnum(InvoiceType), default=InvoiceType.GST_B2C)
    status = Column(SAEnum(InvoiceStatus), default=InvoiceStatus.GENERATED)
    business_name = Column(String(200), nullable=True)
    business_address = Column(Text, nullable=True)
    gstin = Column(String(50), nullable=True)
    taxable_amount = Column(Float, default=0.0)
    cgst_amount = Column(Float, default=0.0)
    sgst_amount = Column(Float, default=0.0)
    igst_amount = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    balance_amount = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    pdf_url = Column(String(500), nullable=True)
    sent_email_at = Column(DateTime, nullable=True)
    sent_whatsapp_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
