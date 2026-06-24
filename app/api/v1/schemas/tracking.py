from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class UpdateLocationRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    technician_id: Optional[str] = None
    booking_id: Optional[str] = None
    accuracy: Optional[float] = Field(None, ge=0)
    speed: Optional[float] = Field(None, ge=0)
    heading: Optional[float] = Field(None, ge=0, le=360)
    source: str = "MOBILE_APP"
    recorded_at: Optional[datetime] = None
