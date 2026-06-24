from sqlalchemy import Column, String, Text, Float
from app.models.base import BaseModel, BaseModel as BM
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey

class Vendor(BM):
    __tablename__   = "vendors"
    name            = Column(String(200), nullable=False)
    contact_person  = Column(String(150), nullable=True)
    mobile          = Column(String(20), nullable=True)
    email           = Column(String(200), nullable=True)
    gstin           = Column(String(20), nullable=True)
    address         = Column(Text, nullable=True)

class VendorTransaction(BM):
    __tablename__ = "vendor_transactions"
    vendor_id     = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    amount        = Column(Float, nullable=False)
    type          = Column(String(30), nullable=False)
    notes         = Column(Text, nullable=True)
