from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import BaseModel


class TrackingLocation(BaseModel):
    __tablename__ = "tracking_locations"

    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    accuracy = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    source = Column(String(50), default="MOBILE_APP", nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
