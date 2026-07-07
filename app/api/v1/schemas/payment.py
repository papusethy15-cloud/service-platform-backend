from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class CreateOrderRequest(BaseModel):
    invoice_id: str
    amount: Optional[float] = None
    notes: Optional[str] = None


class VerifyPaymentRequest(BaseModel):
    transaction_id: str
    provider_payment_id: str
    provider_signature: Optional[str] = None
    amount: Optional[float] = None
    notes: Optional[str] = None


class CashPaymentRequest(BaseModel):
    invoice_id: str
    amount: float
    reference_number: Optional[str] = None
    notes: Optional[str] = None
    # Admin acting on behalf of technician: pass the technician's USER id here.
    # When set, backend creates a PENDING CashCollectionRecord assigned to that
    # technician — same as if the technician collected it themselves.
    on_behalf_technician_id: Optional[str] = None
    # ── Pay Later ────────────────────────────────────────────────────────────
    # Set True to record this as a deferred payment instead of a real cash
    # collection. Requires due_collect_at. Replaces the old
    # reference_number == "PAY_LATER" sentinel.
    is_pay_later: bool = False
    due_collect_at: Optional[datetime] = None


class BankTransferPaymentRequest(BaseModel):
    invoice_id: str
    amount: float
    reference_number: str
    notes: Optional[str] = None


class GeneratePaymentLinkRequest(BaseModel):
    invoice_id: str
    amount: Optional[float] = None
    notes: Optional[str] = None


class GeneratePaymentQRRequest(BaseModel):
    invoice_id: str
    amount: Optional[float] = None
    notes: Optional[str] = None
