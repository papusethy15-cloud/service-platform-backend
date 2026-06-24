from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel

class WarrantyStatus(str, enum.Enum):
    ACTIVE   = "ACTIVE"
    EXPIRED  = "EXPIRED"
    CLAIMED  = "CLAIMED"

class ClaimStatus(str, enum.Enum):
    PENDING  = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESOLVED = "RESOLVED"

class Warranty(BaseModel):
    __tablename__ = "warranties"
    customer_id    = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    booking_id     = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    warranty_type  = Column(String(30), default="SERVICE")
    description    = Column(Text, nullable=False)
    expiry_date    = Column(DateTime, nullable=False)
    parts_covered  = Column(Text, nullable=True)
    status         = Column(SAEnum(WarrantyStatus), default=WarrantyStatus.ACTIVE)

class WarrantyClaim(BaseModel):
    __tablename__ = "warranty_claims"
    warranty_id  = Column(UUID(as_uuid=True), ForeignKey("warranties.id"), nullable=False)
    claimed_by   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    booking_id   = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    description  = Column(Text, nullable=False)
    status       = Column(SAEnum(ClaimStatus), default=ClaimStatus.PENDING)
    approved_by  = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    rejected_by  = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    notes        = Column(Text, nullable=True)
