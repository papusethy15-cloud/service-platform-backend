"""CCO Attendance and Salary models."""
import uuid
from sqlalchemy import Column, String, Text, ForeignKey, DateTime, Date, Float, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base


class CcoAttendance(Base):
    """Daily attendance record for a CCO user.

    One row per (user_id, date). Multiple login sessions in a day are merged
    into accumulated_seconds so admin sees total hours per day.
    check_in = last session start (or first of day if never checked out yet).
    check_out = last session end (None if currently active).
    """
    __tablename__ = "cco_attendance"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_cco_attendance_user_date"),)

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id              = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date                 = Column(Date, nullable=False)
    check_in             = Column(DateTime(timezone=True), nullable=True)
    check_out            = Column(DateTime(timezone=True), nullable=True)
    accumulated_seconds  = Column(Integer, nullable=False, default=0)
    status               = Column(String(20), nullable=False, default="PRESENT")
    notes                = Column(Text, nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())


class CcoSalarySettlement(Base):
    """Monthly salary settlement record for a CCO."""
    __tablename__ = "cco_salary_settlements"
    __table_args__ = (UniqueConstraint("user_id", "month", "year", name="uq_cco_salary_user_month_year"),)

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id              = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    month                = Column(Integer, nullable=False)
    year                 = Column(Integer, nullable=False)

    monthly_salary       = Column(Float, nullable=False, default=0)
    petrol_amount        = Column(Float, nullable=False, default=0)
    mobile_recharge      = Column(Float, nullable=False, default=0)
    bonus_amount         = Column(Float, nullable=False, default=0)
    hra_amount           = Column(Float, nullable=False, default=0)
    other_allowances     = Column(Float, nullable=False, default=0)
    deductions           = Column(Float, nullable=False, default=0)
    deduction_notes      = Column(String(500), nullable=True)

    total_days           = Column(Integer, nullable=False, default=0)
    present_days         = Column(Integer, nullable=False, default=0)
    total_hours          = Column(Float,   nullable=False, default=0)

    gross_salary         = Column(Float, nullable=False, default=0)
    net_salary           = Column(Float, nullable=False, default=0)

    status               = Column(String(20), nullable=False, default="PENDING")  # PENDING | PAID
    payment_method       = Column(String(20), nullable=True)
    payment_ref          = Column(String(200), nullable=True)
    paid_at              = Column(DateTime(timezone=True), nullable=True)
    paid_by              = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    salary_notes         = Column(Text, nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
