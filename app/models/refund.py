import uuid
from sqlalchemy import Column, String, Float, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class Refund(Base):
    __tablename__ = "refunds"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payment_transactions.id"))
    amount = Column(Float, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(20), default="PENDING")  # PENDING, APPROVED, PROCESSED, REJECTED
    refund_method = Column(String(30))  # ORIGINAL, WALLET, BANK
    processed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    processed_at = Column(DateTime(timezone=True))
    gateway_refund_id = Column(String(200))
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
