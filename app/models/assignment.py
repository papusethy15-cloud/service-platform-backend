from sqlalchemy import Column, String, Float, Integer, Boolean, Text, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class AssignmentType(str, enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class AssignmentStatus(str, enum.Enum):
    ASSIGNED = "ASSIGNED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    REASSIGNED = "REASSIGNED"


class AssignmentRule(BaseModel):
    __tablename__ = "assignment_rules"

    name = Column(String(100), unique=True, nullable=False, default="default")
    strategy = Column(String(100), default="SKILL_RATING_WORKLOAD")
    max_active_bookings = Column(Integer, default=3)
    prefer_same_city = Column(Boolean, default=True)
    require_skill_match = Column(Boolean, default=True)
    prefer_high_rating = Column(Boolean, default=True)
    prefer_low_workload = Column(Boolean, default=True)
    response_timeout_minutes = Column(Integer, default=5)
    notes = Column(Text, nullable=True)


class AssignmentHistory(BaseModel):
    __tablename__ = "assignment_history"

    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    assignment_type = Column(SAEnum(AssignmentType), nullable=False)
    status = Column(SAEnum(AssignmentStatus), nullable=False, default=AssignmentStatus.ASSIGNED)
    score = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
