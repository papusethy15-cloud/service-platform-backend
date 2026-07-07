from pydantic import BaseModel
from typing import Optional, List, Union
from datetime import datetime, date

class CreateBookingRequest(BaseModel):
    # Admin/CCO can pass customer_id explicitly
    customer_id:     Optional[str] = None
    service_id:      Optional[str] = None      # FK to services; optional when using free-text
    service_name:    Optional[str] = None      # free-text fallback (chatbot / website)
    address_id:      Optional[str] = None      # FK to customer addresses; optional for new addresses
    address_line:    Optional[str] = None      # free-text address fallback
    city:            Optional[str] = None
    scheduled_date:  datetime
    scheduled_slot:  Optional[str] = None
    notes:           Optional[str] = None
    appliance_brand: Optional[str] = None
    appliance_model: Optional[str] = None
    source:          str = "CALL_CENTER"
    priority:        Optional[str] = "NORMAL"
    domain_id:       Optional[str] = None
    city_id:         Optional[str] = None
    # Duplicate override: pass True to allow booking same service+address
    force_duplicate: Optional[bool] = False
    # Coupon fields (optional — sent when customer applies a coupon code)
    coupon_id:       Optional[str] = None
    coupon_code:     Optional[str] = None
    coupon_discount: Optional[float] = 0.0
    base_amount:     Optional[float] = None

class UpdateBookingRequest(BaseModel):
    scheduled_date:  Optional[datetime] = None
    scheduled_slot:  Optional[str] = None
    notes:           Optional[str] = None
    priority:        Optional[str] = None

class SubmitInspectionRequest(BaseModel):
    notes:      str
    photo_urls: List[str] = []
    # When CCO or admin submits inspection on behalf of the technician
    submitted_by_role: Optional[str] = None  # 'TECHNICIAN' | 'CCO' | 'ADMIN'

class RescheduleBookingRequest(BaseModel):
    scheduled_date:  Union[datetime, date]
    scheduled_slot:  Optional[str] = None
    reason:          Optional[str] = None

class AssignTechnicianRequest(BaseModel):
    technician_id: str
    notes:         Optional[str] = None

class VisitingChargeRequest(BaseModel):
    amount: float
    notes: str = "Customer declined repair — visiting charge applied"

class CancelBookingRequest(BaseModel):
    reason: str

class BookingResponse(BaseModel):
    id:              str
    booking_number:  str
    status:          str
    customer_id:     str
    technician_id:   Optional[str]
    service_id:      Optional[str]
    scheduled_date:  datetime
    scheduled_slot:  Optional[str]
    notes:           Optional[str]
    appliance_brand: Optional[str]
    appliance_model: Optional[str]
    base_amount:     float
    gst_amount:      float
    total_amount:    float
    priority:        str
    created_at:      datetime
    class Config:
        from_attributes = True

class BookingStatusLogResponse(BaseModel):
    id:         str
    status:     str
    notes:      Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True
