from sqlalchemy import Column, String, Float, Integer, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel


class City(BaseModel):
    __tablename__ = "cities"
    name               = Column(String(100), nullable=False, unique=True)
    state              = Column(String(100), nullable=False)
    country            = Column(String(100), default="India")
    base_travel_charge = Column(Float, default=0.0)
    surge_multiplier   = Column(Float, default=1.0)
    sort_order         = Column(Integer, default=0)
    # Extended fields
    latitude           = Column(Float, nullable=True)
    longitude          = Column(Float, nullable=True)
    is_serviceable     = Column(Boolean, default=True)   # city active for bookings


class Zone(BaseModel):
    """Logical sub-division of a city (e.g. North Bhubaneswar, South Bhubaneswar)."""
    __tablename__ = "zones"
    city_id      = Column(UUID(as_uuid=True), ForeignKey("cities.id"), nullable=False)
    name         = Column(String(150), nullable=False)
    description  = Column(Text, nullable=True)


class Area(BaseModel):
    """Specific locality / neighbourhood within a city or zone."""
    __tablename__ = "areas"
    city_id          = Column(UUID(as_uuid=True), ForeignKey("cities.id"),  nullable=False)
    zone_id          = Column(UUID(as_uuid=True), ForeignKey("zones.id"),   nullable=True)
    name             = Column(String(150), nullable=False)
    pincode          = Column(String(20),  nullable=True)
    latitude         = Column(Float,       nullable=True)
    longitude        = Column(Float,       nullable=True)
    surge_multiplier = Column(Float, default=1.0)   # area-level override


class CitySettings(BaseModel):
    """Per-city operational settings."""
    __tablename__ = "city_settings"
    __table_args__ = (UniqueConstraint("city_id", name="uq_city_settings"),)
    city_id                  = Column(UUID(as_uuid=True), ForeignKey("cities.id"), nullable=False)
    min_booking_amount       = Column(Float, default=0.0)
    max_booking_amount       = Column(Float, nullable=True)
    booking_advance_days     = Column(Integer, default=7)   # how far ahead customer can book
    cancellation_window_hrs  = Column(Integer, default=2)   # free cancel within N hours
    auto_assign_enabled      = Column(Boolean, default=True)
    notes                    = Column(Text, nullable=True)
