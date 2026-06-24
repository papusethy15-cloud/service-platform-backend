from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class CreateInternalUserRequest(BaseModel):
    name: str
    mobile: str
    email: Optional[EmailStr] = None
    password: str = Field(..., min_length=6)
    role: str
    city: Optional[str] = None


class UpdateInternalUserRequest(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=6)
    role: Optional[str] = None
    city: Optional[str] = None
    is_verified: Optional[bool] = None
    is_active: Optional[bool] = None


class UpdateRolePermissionsRequest(BaseModel):
    permission_codes: List[str]


class UserPermissionOverrideItem(BaseModel):
    permission_code: str
    is_granted: bool = True


class UpdateUserPermissionsRequest(BaseModel):
    overrides: List[UserPermissionOverrideItem]
