from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel

class EscalationStatus(str, enum.Enum):
    OPEN        = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    ESCALATED   = "ESCALATED"
    RESOLVED    = "RESOLVED"
    CLOSED      = "CLOSED"

class Escalation(BaseModel):
    __tablename__      = "escalations"
    created_by         = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    booking_id         = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    subject            = Column(String(300), nullable=False)
    description        = Column(Text, nullable=False)
    priority           = Column(String(20), default="MEDIUM")
    status             = Column(SAEnum(EscalationStatus), default=EscalationStatus.OPEN)
    assigned_to        = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_by        = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_at        = Column(DateTime, nullable=True)
    resolution_notes   = Column(Text, nullable=True)
    escalation_level   = Column(Integer, default=1)
    escalation_notes   = Column(Text, nullable=True)
