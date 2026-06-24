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
    service_id: str
    quantity: int = 1
    unit_price: Optional[float] = None
    appliance_label: Optional[str] = None


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
