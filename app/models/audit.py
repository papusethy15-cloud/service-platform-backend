import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id       = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    user_name     = Column(String(150), nullable=True)
    user_role     = Column(String(50), nullable=True)
    action        = Column(String(100), nullable=False)
    resource_type = Column(String(100), nullable=True)
    resource_id   = Column(String(200), nullable=True)
    description   = Column(String(500), nullable=True)
    old_data      = Column(JSONB, nullable=True)
    new_data      = Column(JSONB, nullable=True)
    ip_address    = Column(String(50), nullable=True)
    user_agent    = Column(String(500), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
