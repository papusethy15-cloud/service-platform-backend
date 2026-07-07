from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
from app.utils.phone import normalize_mobile

class SendOTPRequest(BaseModel):
    mobile: str
    country_code: str = "+91"

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)

class VerifyOTPRequest(BaseModel):
    mobile: str
    otp: str

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)

class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    name: str

class FirebaseLoginRequest(BaseModel):
    firebase_id_token: str

class VerifyOTPFirebaseRequest(BaseModel):
    mobile: str
    otp: str
    firebase_id_token: str

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)

class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    city: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class ForgotPasswordRequest(BaseModel):
    mobile: str

    @field_validator("mobile", mode="before")
    @classmethod
    def _normalize_mobile(cls, v):
        return normalize_mobile(v)

class UserResponse(BaseModel):
    id: str
    name: str
    mobile: str
    email: Optional[str]
    role: str
    city: Optional[str]
    is_verified: bool
