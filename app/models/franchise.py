import uuid
from sqlalchemy import Column, String, Boolean, Text, ForeignKey, DateTime, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class Franchise(Base):
    __tablename__ = "franchises"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    city = Column(String(100))
    state = Column(String(100))
    address = Column(Text)
    phone = Column(String(20))
    email = Column(String(200))
    commission_rate = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
