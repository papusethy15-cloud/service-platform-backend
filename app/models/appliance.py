"""
Appliance Models
SRS §6: customer_appliances with brand, type, warranty, service history
DB spec §5: appliance_categories, appliance_brands, appliance_models,
            customer_appliances, appliance_images, appliance_warranty,
            appliance_service_history, appliance_installation_history
"""
import uuid
import enum
from sqlalchemy import Column, String, Boolean, Text, ForeignKey, DateTime, Integer, Float, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.models.base import Base


class ApplianceStatus(str, enum.Enum):
    ACTIVE      = "ACTIVE"
    UNDER_REPAIR= "UNDER_REPAIR"
    SCRAPPED    = "SCRAPPED"
    SOLD        = "SOLD"
    INACTIVE    = "INACTIVE"


APPLIANCE_CATEGORIES = [
    "AC", "Refrigerator", "Washing Machine", "Microwave",
    "Water Purifier", "Geyser", "Deep Freezer", "Air Cooler", "Other"
]


class ApplianceBrand(Base):
    __tablename__ = "appliance_brands"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name       = Column(String(100), nullable=False, unique=True)
    logo_url   = Column(String(500))
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())




class BrandCategory(Base):
    """
    Many-to-many: which appliance categories a brand operates in.
    e.g. LG → AC, Refrigerator, Washing Machine
    """
    __tablename__ = "brand_categories"
    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id             = Column(UUID(as_uuid=True), ForeignKey("appliance_brands.id"), nullable=False, index=True)
    appliance_category_id= Column(UUID(as_uuid=True), ForeignKey("service_categories.id"), nullable=False)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
class ApplianceType(Base):
    """Appliance type / model template (e.g. 1.5 Ton Split AC).
    appliance_category_id → service_categories.id  (unified category)
    category (str) kept for backward-compat / quick display.
    """
    __tablename__ = "appliance_types"
    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name                 = Column(String(100), nullable=False)
    category             = Column(String(100))           # display label e.g. "AC"
    appliance_category_id= Column(UUID(as_uuid=True), ForeignKey("service_categories.id"))
    brand_id             = Column(UUID(as_uuid=True), ForeignKey("appliance_brands.id"))
    is_active            = Column(Boolean, default=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())


class CustomerAppliance(Base):
    """
    A physical appliance owned by a customer.
    Linked to warranty, service bookings, AMC.
    appliance_category_id → service_categories.id for domain/service filtering.
    """
    __tablename__ = "customer_appliances"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id          = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    brand_id             = Column(UUID(as_uuid=True), ForeignKey("appliance_brands.id"))
    type_id              = Column(UUID(as_uuid=True), ForeignKey("appliance_types.id"))
    appliance_category_id= Column(UUID(as_uuid=True), ForeignKey("service_categories.id"))
    category             = Column(String(100))  # display label e.g. "AC"
    model                = Column(String(200))
    serial_number    = Column(String(200))
    purchase_date    = Column(DateTime(timezone=True))
    installation_date= Column(DateTime(timezone=True))
    warranty_expiry  = Column(DateTime(timezone=True))
    status           = Column(String(30), default="ACTIVE")  # stored as VARCHAR in DB
    notes            = Column(Text)
    image_url        = Column(String(500))
    is_active        = Column(Boolean, default=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())


class ApplianceServiceHistory(Base):
    """Links a customer appliance to a past completed booking."""
    __tablename__ = "appliance_service_history"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appliance_id  = Column(UUID(as_uuid=True), ForeignKey("customer_appliances.id"), nullable=False)
    booking_id    = Column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    service_date  = Column(DateTime(timezone=True), server_default=func.now())
    issue_reported= Column(Text)
    work_done     = Column(Text)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
