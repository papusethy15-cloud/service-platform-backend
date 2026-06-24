from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.core.config import settings
from app.models.user import User
from app.api.v1.schemas.auth import (
    SendOTPRequest, VerifyOTPRequest, LoginRequest,
    RefreshTokenRequest, UpdateProfileRequest,
    ChangePasswordRequest, ForgotPasswordRequest
)
from app.api.deps import get_current_user
from app.utils.otp import generate_otp, get_otp_redis_key
from app.utils.response import success_response
import redis.asyncio as aioredis

router = APIRouter()

async def get_redis():
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)

async def _issue_otp(mobile: str) -> str:
    otp = generate_otp()
    r = await get_redis()
    key = get_otp_redis_key(mobile)
    await r.setex(key, settings.OTP_EXPIRE_MINUTES * 60, otp)
    return otp

# BUG FIX: duplicate settings import removed; single consistent import
@router.post("/send-otp", summary="Send OTP to mobile number")
async def send_otp(payload: SendOTPRequest, db: AsyncSession = Depends(get_db)):
    otp = await _issue_otp(payload.mobile)
    # TODO: Integrate SMS gateway — remove otp from response in production
    return success_response(
        data={"otp": otp, "expires_in": f"{settings.OTP_EXPIRE_MINUTES} minutes"},
        message="OTP sent successfully"
    )

@router.post("/verify-otp", summary="Verify OTP — returns JWT tokens")
async def verify_otp(payload: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    r = await get_redis()
    key = get_otp_redis_key(payload.mobile)
    stored_otp = await r.get(key)
    if not stored_otp or stored_otp != payload.otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")
    await r.delete(key)
    result = await db.execute(select(User).where(User.mobile == payload.mobile))
    user = result.scalar_one_or_none()
    if not user:
        user = User(name="New User", mobile=payload.mobile, is_verified=True)
        db.add(user)
        await db.flush()
    user.is_verified = True
    await db.commit()  # BUG FIX: was missing commit — new user was never persisted
    access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
    refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
    return success_response(data={
        "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "bearer", "user_id": str(user.id),
        "role": user.role.value, "name": user.name
    }, message="Login successful")

@router.post("/login", summary="Email + password login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
    refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
    return success_response(data={
        "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "bearer", "user_id": str(user.id),
        "role": user.role.value, "name": user.name
    }, message="Login successful")

@router.post("/refresh-token", summary="Get new access token using refresh token")
async def refresh_token_endpoint(payload: RefreshTokenRequest):
    # BUG FIX: db dependency removed — token refresh doesn't need DB; renamed to avoid shadowing stdlib
    from jose import JWTError
    try:
        data = decode_token(payload.refresh_token)
        user_id = data.get("sub")
        role = data.get("role")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    access_token = create_access_token({"sub": user_id, "role": role})
    return success_response(data={"access_token": access_token, "token_type": "bearer"})

@router.post("/logout", summary="Logout user")
async def logout(current_user: dict = Depends(get_current_user)):
    # TODO: Blacklist token in Redis for true stateless invalidation
    return success_response(message="Logged out successfully")

@router.get("/profile", summary="Get my profile")
async def get_profile(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(current_user["user_id"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return success_response(data={
        "id": str(user.id), "name": user.name, "mobile": user.mobile,
        "email": user.email, "role": user.role.value,
        "city": user.city, "profile_image": user.profile_image,
        "is_verified": user.is_verified, "created_at": user.created_at.isoformat()
    })

@router.put("/profile", summary="Update my profile")
async def update_profile(
    payload: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(current_user["user_id"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.name:  user.name = payload.name
    if payload.email: user.email = payload.email
    if payload.city:  user.city = payload.city
    await db.commit()  # BUG FIX: missing commit — updates were not saved
    return success_response(message="Profile updated successfully")

@router.post("/change-password", summary="Change password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(current_user["user_id"])))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.current_password, user.password_hash or ""):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    await db.commit()  # BUG FIX: missing commit
    return success_response(message="Password changed successfully")

@router.post("/forgot-password", summary="Send OTP for password recovery")
async def forgot_password(payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.mobile == payload.mobile))
    user = result.scalar_one_or_none()
    # BUG FIX: original raised 404 which exposes whether mobile is registered (security issue)
    # Return success regardless to prevent user enumeration
    if user:
        otp = await _issue_otp(payload.mobile)
        # TODO: Send via SMS gateway
    else:
        otp = None  # don't reveal non-existence in production; for dev we show nothing
    return success_response(
        data={"expires_in": f"{settings.OTP_EXPIRE_MINUTES} minutes"},
        message="If the mobile is registered, an OTP has been sent"
    )
