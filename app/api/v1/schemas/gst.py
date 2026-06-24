from typing import Optional

from pydantic import BaseModel, Field


class UpdateGSTSettingsRequest(BaseModel):
    gst_enabled: Optional[bool] = None
    default_rate: Optional[float] = Field(None, ge=0, le=100)
    allow_b2b: Optional[bool] = None
    allow_b2c: Optional[bool] = None
    allow_non_gst: Optional[bool] = None
    gstin_validation_enabled: Optional[bool] = None
    company_gstin: Optional[str] = None
    company_name: Optional[str] = None
    company_address: Optional[str] = None
    hsn_code: Optional[str] = None
    invoice_prefix: Optional[str] = None
    state_code: Optional[str] = None


class ValidateGSTINRequest(BaseModel):
    gstin: str
