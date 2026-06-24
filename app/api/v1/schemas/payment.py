from pydantic import BaseModel
from typing import Optional


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
