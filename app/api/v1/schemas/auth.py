from pydantic import BaseModel, EmailStr
from typing import Optional

class SendOTPRequest(BaseModel):
    mobile: str
    country_code: str = "+91"

class VerifyOTPRequest(BaseModel):
    mobile: str
    otp: str

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

class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    city: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class ForgotPasswordRequest(BaseModel):
    mobile: str

class UserResponse(BaseModel):
    id: str
    name: str
    mobile: str
    email: Optional[str]
    role: str
    city: Optional[str]
    is_verified: bool
