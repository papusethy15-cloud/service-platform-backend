import uuid
from sqlalchemy import Column, String, Boolean, Integer, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class SLAPolicy(Base):
    __tablename__ = "sla_policies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    response_time_minutes = Column(Integer)  # Max time to assign
    resolution_time_hours = Column(Integer)  # Max time to complete
    priority = Column(String(20), default="STANDARD")  # STANDARD, PRIORITY, EMERGENCY
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SLABreach(Base):
    __tablename__ = "sla_breaches"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    policy_id = Column(UUID(as_uuid=True), ForeignKey("sla_policies.id"))
    breach_type = Column(String(30))  # RESPONSE, RESOLUTION
    breached_at = Column(DateTime(timezone=True), server_default=func.now())
    notes = Column(Text)
