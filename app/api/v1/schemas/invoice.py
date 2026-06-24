from pydantic import BaseModel
from typing import Optional


class CreateInvoiceRequest(BaseModel):
    quotation_id: str
    invoice_type: str = "GST_B2C"
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    gstin: Optional[str] = None
    notes: Optional[str] = None


class InvoiceSendRequest(BaseModel):
    recipient: Optional[str] = None
    notes: Optional[str] = None
