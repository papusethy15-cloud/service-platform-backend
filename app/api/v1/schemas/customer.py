from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
from app.utils.phone import normalize_mobile

class CreateCustomerRequest(BaseModel):
    name: str
    mobile: str
    email: Optional[EmailStr] = None
    alternate_mobile: Optional[str] = None
    city: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)

    @field_validator("alternate_mobile", mode="before")
    @classmethod
    def _normalize_alternate_mobile(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return normalize_mobile(v)

class UpdateCustomerRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    alternate_mobile: Optional[str] = None
    notes: Optional[str] = None
    gst_number: Optional[str] = None
    gst_name: Optional[str] = None
    gst_address: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v):
        """Convert empty string to None so EmailStr validation is skipped."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("gst_number", "gst_name", "gst_address", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        """Convert empty strings to None for optional string fields."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("alternate_mobile", mode="before")
    @classmethod
    def _normalize_alternate_mobile(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return normalize_mobile(v)

class CustomerAddressRequest(BaseModel):
    label: str = "Home"
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: str
    pincode: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_default: bool = False
    location_source: Optional[str] = None  # 'gps'|'geocoded'|'manual'|'whatsapp'

class CustomerResponse(BaseModel):
    id: str
    name: str
    mobile: str
    email: Optional[str]
    customer_code: Optional[str]
    city: Optional[str]
    total_bookings: Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True

class CustomerAddressResponse(BaseModel):
    id: str
    label: str
    address_line1: str
    address_line2: Optional[str]
    city: str
    state: str
    pincode: str
    latitude: Optional[float]
    longitude: Optional[float]
    is_default: bool
    class Config:
        from_attributes = True
