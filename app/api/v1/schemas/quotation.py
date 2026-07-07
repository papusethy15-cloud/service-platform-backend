from pydantic import BaseModel
from typing import Optional


class CreateQuotationRequest(BaseModel):
    booking_id: str
    labour_charges: float = 0.0
    service_charges: float = 0.0
    tax_percent: float = 18.0
    tax_mode: str = 'B2C'           # NONE | B2C | B2B
    customer_gst_number:  Optional[str] = None
    customer_gst_name:    Optional[str] = None
    customer_gst_address: Optional[str] = None
    remarks: Optional[str] = None
    coupon_code: Optional[str] = None


class UpdateQuotationRequest(BaseModel):
    labour_charges:       Optional[float] = None
    service_charges:      Optional[float] = None
    tax_percent:          Optional[float] = None
    tax_mode:             Optional[str]   = None   # NONE | B2C | B2B
    customer_gst_number:  Optional[str]   = None
    customer_gst_name:    Optional[str]   = None
    customer_gst_address: Optional[str]   = None
    remarks:              Optional[str]   = None


class AddQuotationServiceRequest(BaseModel):
    service_id: Optional[str] = None       # None when adding a custom/new service
    quantity: int = 1
    unit_price: Optional[float] = None
    appliance_label: Optional[str] = None
    # Custom service fields (used when service_id is None)
    custom_service_name: Optional[str] = None   # name technician typed
    custom_base_price:   Optional[float] = None  # price technician set


class VerifyCustomServiceRequest(BaseModel):
    """Admin: promote a tech-suggested service item to the catalogue."""
    category_id:   str           # which category to put the new service in
    name:          str           # final service name (admin may rename)
    base_price:    float         # official base price
    gst_percent:   float = 18.0
    duration_mins: int   = 60
    is_visible:    bool  = True
    domain_id:     Optional[str] = None   # link to domain if needed
    # Commission to set for this service in the technician's group
    commission_type:  Optional[str]   = None   # PERCENTAGE | FLAT
    commission_value: Optional[float] = None


class AddQuotationPartRequest(BaseModel):
    part_name: str
    part_source: str = "OFFICE_STOCK"
    quantity: int = 1
    unit_price: float
    purchase_price: Optional[float] = None
    vendor_name: Optional[str] = None
    bill_number: Optional[str] = None
    notes: Optional[str] = None
    appliance_label: Optional[str] = None
    inventory_item_id: Optional[str] = None
    is_new_part: bool = False


class UpdateQuotationPartRequest(BaseModel):
    part_name: Optional[str] = None
    part_source: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[float] = None
    vendor_name: Optional[str] = None
    bill_number: Optional[str] = None
    notes: Optional[str] = None


class ApplyDiscountRequest(BaseModel):
    amount: float
    notes: Optional[str] = None


class ApplyAdjustmentRequest(BaseModel):
    amount: float
    notes: Optional[str] = None


class QuotationActionRequest(BaseModel):
    notes: Optional[str] = None
    reason: Optional[str] = None
