from pydantic import BaseModel
from typing import Optional


class AutoAssignmentRequest(BaseModel):
    booking_id: str
    notes: Optional[str] = None


class ManualAssignmentRequest(BaseModel):
    booking_id: str
    technician_id: str
    notes: Optional[str] = None


class UpdateAssignmentRuleRequest(BaseModel):
    strategy: Optional[str] = None
    max_active_bookings: Optional[int] = None
    prefer_same_city: Optional[bool] = None
    require_skill_match: Optional[bool] = None
    prefer_high_rating: Optional[bool] = None
    prefer_low_workload: Optional[bool] = None
    response_timeout_minutes: Optional[int] = None
    notes: Optional[str] = None
