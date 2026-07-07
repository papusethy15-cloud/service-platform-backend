from sqlalchemy import Column, String, Text, Float, Boolean, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel

class ServiceCategory(BaseModel):
    __tablename__ = "service_categories"
    name         = Column(String(150), nullable=False)
    description  = Column(Text, nullable=True)
    icon         = Column(String(500), nullable=True)
    sort_order   = Column(Integer, default=0)

class Service(BaseModel):
    __tablename__ = "services"
    category_id         = Column(UUID(as_uuid=True), ForeignKey("service_categories.id"), nullable=False)
    name                = Column(String(200), nullable=False)
    description         = Column(Text, nullable=True)
    base_price          = Column(Float, nullable=False, default=0.0)
    gst_percent         = Column(Float, default=18.0)
    duration_mins       = Column(Integer, default=60)
    is_visible          = Column(Boolean, default=True)
    sort_order          = Column(Integer, default=0)
    # Tech-suggested services: pending admin verification before going live
    # 0 = normal verified service, 1 = pending admin review, 2 = rejected by admin
    is_pending_verify   = Column(Integer, default=0, nullable=False)
    suggested_by_tech   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
