from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel

class CallLog(BaseModel):
    __tablename__ = "call_logs"
    customer_id      = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    cco_id           = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    booking_id       = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    direction        = Column(String(20), default="INBOUND")   # INBOUND | OUTBOUND
    duration_seconds = Column(Integer, nullable=True)
    outcome          = Column(String(40), nullable=False)      # RESOLVED | TICKET_RAISED | NO_ANSWER | CALLBACK_REQUESTED | PAYMENT_REMINDER | OTHER
    summary          = Column(Text, nullable=False)

class CRMNote(BaseModel):
    __tablename__ = "crm_notes"
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    added_by    = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    note        = Column(Text, nullable=False)
    note_type   = Column(String(30), default="GENERAL")

class CRMFollowup(BaseModel):
    __tablename__ = "crm_followups"
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    created_by  = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject     = Column(String(200), nullable=False)
    notes       = Column(Text, nullable=True)
    due_date    = Column(DateTime, nullable=False)
    status      = Column(String(20), default="PENDING")

class CRMTask(BaseModel):
    __tablename__ = "crm_tasks"
    created_by  = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    title       = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    due_date    = Column(DateTime, nullable=True)
    priority    = Column(String(20), default="MEDIUM")
    status      = Column(String(20), default="OPEN")
