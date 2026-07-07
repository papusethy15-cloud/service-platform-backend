from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator
from app.utils.phone import normalize_mobile


class CreateInternalUserRequest(BaseModel):
    name: str
    mobile: str
    email: Optional[EmailStr] = None
    password: str = Field(..., min_length=6)
    role: str
    city: Optional[str] = None
    profile_image: Optional[str] = None
    id_proof_url: Optional[str] = None
    id_proof_type: Optional[str] = None
    address_proof_url: Optional[str] = None
    address_proof_type: Optional[str] = None

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)


class UpdateInternalUserRequest(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=6)
    role: Optional[str] = None
    city: Optional[str] = None
    is_verified: Optional[bool] = None
    is_active: Optional[bool] = None
    profile_image: Optional[str] = None
    id_proof_url: Optional[str] = None
    id_proof_type: Optional[str] = None
    address_proof_url: Optional[str] = None
    address_proof_type: Optional[str] = None

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return normalize_mobile(v)


class UpdateRolePermissionsRequest(BaseModel):
    permission_codes: List[str]


class UserPermissionOverrideItem(BaseModel):
    permission_code: str
    is_granted: bool = True


class UpdateUserPermissionsRequest(BaseModel):
    overrides: List[UserPermissionOverrideItem]
