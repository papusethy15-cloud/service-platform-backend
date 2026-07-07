import uuid
from sqlalchemy import Column, String, Boolean, Text, ForeignKey, DateTime, Date, Time, Float, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    date = Column(Date, nullable=False)
    check_in = Column(DateTime(timezone=True))
    check_out = Column(DateTime(timezone=True))
    check_in_lat = Column(Float)
    check_in_lng = Column(Float)
    accumulated_seconds = Column(Integer, nullable=False, default=0)  # total worked time today across all sessions
    status = Column(String(20), default="PRESENT")  # PRESENT, ABSENT, HALF_DAY, LEAVE
    notes = Column(Text)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    leave_type = Column(String(30))  # SICK, CASUAL, ANNUAL, UNPAID
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=False)
    reason = Column(Text)
    status = Column(String(20), default="PENDING")  # PENDING, APPROVED, REJECTED
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
