from sqlalchemy import Column, String, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class CallbackStatus(str, enum.Enum):
    PENDING  = "PENDING"
    CALLED   = "CALLED"
    RESOLVED = "RESOLVED"
    SKIPPED  = "SKIPPED"


class CallbackRequest(BaseModel):
    __tablename__ = "callback_requests"

    mobile       = Column(String(20), nullable=False, index=True)
    name         = Column(String(150), nullable=True)
    message      = Column(Text, nullable=True)          # what customer typed / requested
    source       = Column(String(30), default="CHATBOT") # CHATBOT | WEBSITE | WEBSITE_MODAL
    status       = Column(SAEnum(CallbackStatus), default=CallbackStatus.PENDING)
    admin_notes  = Column(Text, nullable=True)
    called_at    = Column(DateTime, nullable=True)

    # ── Visitor context (only meaningfully populated when the mobile number
    #    doesn't match an existing customer — gives the admin something to
    #    go on before calling an unknown lead) ──────────────────────────────
    domain_id    = Column(UUID(as_uuid=True), nullable=True, index=True)
    page_url     = Column(String(500), nullable=True)   # page the request was raised from
    ip_address   = Column(String(64), nullable=True)     # supports IPv4 + IPv6
    user_agent   = Column(String(500), nullable=True)
    location     = Column(String(255), nullable=True)    # "City, Region, Country" from IP lookup
