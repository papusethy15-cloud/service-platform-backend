from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, date


class CreateTechnicianRequest(BaseModel):
    # Core fields (existing)
    name: str
    mobile: str
    email: Optional[EmailStr] = None
    city: Optional[str] = None
    area: Optional[str] = None
    experience_years: int = 0
    address: Optional[str] = None

    # Extended fields (new)
    alternate_mobile: Optional[str] = None
    dob: Optional[date] = None
    gender: Optional[str] = None                    # MALE | FEMALE | OTHER
    emergency_contact_name: Optional[str] = None
    emergency_contact_mobile: Optional[str] = None
    identity_type: Optional[str] = None             # Aadhaar, PAN, etc.
    identity_number: Optional[str] = None
    pincode: Optional[str] = None

    # Skills to add at creation time
    skills: Optional[List[dict]] = None  # [{ service_id, proficiency }]

    # Availability schedule
    availability: Optional[List[dict]] = None       # [{ day_of_week, start_time, end_time, is_available }]


class UpdateTechnicianRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    alternate_mobile: Optional[str] = None
    city: Optional[str] = None
    area: Optional[str] = None
    experience_years: Optional[int] = None
    address: Optional[str] = None
    pincode: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_mobile: Optional[str] = None
    identity_type: Optional[str] = None
    identity_number: Optional[str] = None
    status: Optional[str] = None
    # When False, technician is skipped by the auto-assign engine entirely
    # (still assignable manually by admin/CCO). Admin-only toggle.
    auto_assign_eligible: Optional[bool] = None


class AddTechnicianSkillRequest(BaseModel):
    service_id: str
    proficiency: str = "INTERMEDIATE"  # BEGINNER | INTERMEDIATE | EXPERT


class TechnicianResponse(BaseModel):
    id: str
    name: str
    mobile: str
    email: Optional[str] = None
    technician_code: Optional[str] = None
    city: Optional[str] = None
    area: Optional[str] = None
    status: str
    experience_years: int
    rating: float
    total_jobs: int
    profile_image: Optional[str] = None
    address: Optional[str] = None
    # Extended
    alternate_mobile: Optional[str] = None
    gender: Optional[str] = None
    pincode: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_mobile: Optional[str] = None
    identity_type: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
