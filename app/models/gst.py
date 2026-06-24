from sqlalchemy import Boolean, Column, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import BaseModel


class GSTSetting(BaseModel):
    __tablename__ = "gst_settings"

    gst_enabled = Column(Boolean, default=True, nullable=False)
    default_rate = Column(Float, default=18.0, nullable=False)
    allow_b2b = Column(Boolean, default=True, nullable=False)
    allow_b2c = Column(Boolean, default=True, nullable=False)
    allow_non_gst = Column(Boolean, default=True, nullable=False)
    gstin_validation_enabled = Column(Boolean, default=True, nullable=False)
    company_gstin = Column(String(50), nullable=True)
    company_name = Column(String(200), nullable=True)
    company_address = Column(Text, nullable=True)
    hsn_code = Column(String(30), nullable=True)
    invoice_prefix = Column(String(20), default="INV", nullable=False)
    state_code = Column(String(10), nullable=True)
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
