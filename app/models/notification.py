import uuid
from sqlalchemy import Column, String, Boolean, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from app.models.base import Base

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    channel = Column(String(20), default="PUSH")  # PUSH, SMS, EMAIL, WHATSAPP
    is_read = Column(Boolean, default=False)
    data = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    channel = Column(String(20), default="PUSH")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
