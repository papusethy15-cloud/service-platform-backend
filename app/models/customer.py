from sqlalchemy import Column, String, Text, ForeignKey, Boolean, Float
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel

class Customer(BaseModel):
    __tablename__ = "customers"
    user_id        = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)
    name           = Column(String(150), nullable=False)
    mobile         = Column(String(20), nullable=False)
    email          = Column(String(200), nullable=True)
    alternate_mobile = Column(String(20), nullable=True)
    notes          = Column(Text, nullable=True)
    customer_code  = Column(String(30), unique=True, nullable=True)
    total_bookings = Column(String(10), default="0")
    fcm_token      = Column(String(500), nullable=True)   # set on login / app open

    # GST / Tax details
    gst_number     = Column(String(20), nullable=True)
    gst_name       = Column(String(200), nullable=True)  # Company name for GST
    gst_address    = Column(Text, nullable=True)

class CustomerAddress(BaseModel):
    __tablename__ = "customer_addresses"
    customer_id   = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    label         = Column(String(50), default="Home")
    address_line1 = Column(String(300), nullable=False)
    address_line2 = Column(String(300), nullable=True)
    city          = Column(String(100), nullable=False)
    state         = Column(String(100), nullable=False)
    pincode       = Column(String(10), nullable=False)
    latitude      = Column(Float, nullable=True)
    longitude     = Column(Float, nullable=True)
    is_default    = Column(Boolean, default=False)
    location_source = Column(String(50), nullable=True)   # 'gps'|'whatsapp'|'manual'|'geocoded'
