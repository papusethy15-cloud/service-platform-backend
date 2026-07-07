from fastapi import APIRouter, Depends, HTTPException, status
from typing import Optional
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.core.config import settings
from app.models.user import User, UserRole
from app.api.v1.schemas.auth import (
    SendOTPRequest, VerifyOTPRequest, LoginRequest,
    RefreshTokenRequest, UpdateProfileRequest,
    ChangePasswordRequest, ForgotPasswordRequest,
    FirebaseLoginRequest, VerifyOTPFirebaseRequest
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

async def _verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return {uid, email, name}. Raises HTTPException on failure."""
    import asyncio
    from app.utils.fcm import get_firebase_app
    try:
        from firebase_admin import auth as firebase_auth
        app = await get_firebase_app()
        if not app:
            raise HTTPException(status_code=500, detail="Firebase Admin SDK is not configured on the server")
        loop = asyncio.get_event_loop()
        decoded = await loop.run_in_executor(None, lambda: firebase_auth.verify_id_token(id_token, app=app))
        return {
            "uid": decoded.get("uid"),
            "email": decoded.get("email"),
            "name": decoded.get("name") or (decoded.get("email", "").split("@")[0] if decoded.get("email") else "New Customer"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired Firebase token: {e}")


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
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been suspended.")
    await db.commit()  # BUG FIX: was missing commit — new user was never persisted
    access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
    refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
    return success_response(data={
        "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "bearer", "user_id": str(user.id),
        "role": user.role.value, "name": user.name
    }, message="Login successful")

@router.post("/firebase-login", summary="Customer app: Google/Firebase sign-in")
async def firebase_login(payload: FirebaseLoginRequest, db: AsyncSession = Depends(get_db)):
    fb = await _verify_firebase_token(payload.firebase_id_token)
    uid, email, name = fb["uid"], fb["email"], fb["name"]

    # Look up an existing account by firebase_uid first, then by email as a fallback
    user = (await db.execute(select(User).where(User.firebase_uid == uid))).scalar_one_or_none()
    if not user and email:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if user:
        if not user.firebase_uid:
            user.firebase_uid = uid
            await db.commit()
        access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
        refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
        return success_response(data={
            "status": "existing",
            "access_token": access_token, "refresh_token": refresh_token,
            "token_type": "bearer", "user_id": str(user.id),
            "role": user.role.value, "name": user.name,
        }, message="Login successful")

    # No matching account yet — do NOT create a User row (mobile is required/unique).
    # Client should hold onto firebase_id_token and proceed to mobile verification.
    return success_response(data={
        "status": "new",
        "firebase_name": name,
        "firebase_email": email,
    }, message="No account found — mobile verification required")


@router.post("/verify-otp-firebase", summary="Customer app: verify OTP and create/link account using Firebase identity")
async def verify_otp_firebase(payload: VerifyOTPFirebaseRequest, db: AsyncSession = Depends(get_db)):
    r = await get_redis()
    key = get_otp_redis_key(payload.mobile)
    stored_otp = await r.get(key)
    if not stored_otp or stored_otp != payload.otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")
    await r.delete(key)

    fb = await _verify_firebase_token(payload.firebase_id_token)
    uid, email, name = fb["uid"], fb["email"], fb["name"]

    # Mobile number is the authoritative customer identity:
    #  - if a customer already exists with this mobile, link this Google
    #    identity to it and refresh the email from Firebase.
    #  - otherwise, register a brand-new customer using the Firebase name/
    #    email plus the now-verified mobile number.
    user = (await db.execute(select(User).where(User.mobile == payload.mobile))).scalar_one_or_none()

    if user:
        user.firebase_uid = uid
        if email:
            user.email = email
        user.is_verified = True
    else:
        user = User(
            name=name or "New Customer",
            mobile=payload.mobile,
            email=email,
            firebase_uid=uid,
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        db.add(user)
        await db.flush()
    await db.commit()

    access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
    refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
    return success_response(data={
        "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "bearer", "user_id": str(user.id),
        "role": user.role.value, "name": user.name,
    }, message="Account verified successfully")


@router.post("/login", summary="Email + password login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been suspended. Please contact an administrator.")
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
    # Rolling session: reissue the refresh token too, with a fresh
    # REFRESH_TOKEN_EXPIRE_DAYS window. Clients call this endpoint whenever
    # the access token expires during active use, so as long as the user
    # keeps using the app, the refresh token's expiry keeps sliding forward.
    # A user who stops using the app for REFRESH_TOKEN_EXPIRE_DAYS straight
    # will have a stale refresh token that this endpoint will reject,
    # naturally enforcing "session expires N days after last activity"
    # without needing a separate last-activity column.
    new_refresh_token = create_refresh_token({"sub": user_id, "role": role})
    return success_response(data={
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
    })

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


# ── Captain App (Technician) Auth ─────────────────────────────────────────────
# Technicians use the SAME OTP flow but the verify step checks for role=TECHNICIAN.
# The admin must have already created their user account with role=TECHNICIAN.

@router.post("/technician/send-otp", summary="Captain: request OTP (technician mobile)")
async def technician_send_otp(payload: SendOTPRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.mobile == payload.mobile))
    user = result.scalar_one_or_none()
    if not user or user.role.value != "TECHNICIAN":
        # Return same message to avoid role enumeration
        return success_response(
            data={"expires_in": f"{settings.OTP_EXPIRE_MINUTES} minutes"},
            message="If the mobile is registered as a technician, an OTP has been sent"
        )
    otp = await _issue_otp(payload.mobile)
    # TODO: send via SMS gateway
    return success_response(
        data={"otp": otp, "expires_in": f"{settings.OTP_EXPIRE_MINUTES} minutes"},
        message="OTP sent"
    )


@router.post("/technician/verify-otp", summary="Captain: verify OTP → JWT (technician)")
async def technician_verify_otp(payload: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    r = await get_redis()
    key = get_otp_redis_key(payload.mobile)
    stored_otp = await r.get(key)
    if not stored_otp or stored_otp != payload.otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")
    await r.delete(key)
    result = await db.execute(select(User).where(User.mobile == payload.mobile))
    user = result.scalar_one_or_none()
    if not user or user.role.value != "TECHNICIAN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a registered technician")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been suspended. Please contact your administrator.")
    user.is_verified = True
    await db.commit()
    access_token  = create_access_token({"sub": str(user.id), "role": user.role.value})
    refresh_token = create_refresh_token({"sub": str(user.id), "role": user.role.value})
    return success_response(data={
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "token_type":    "bearer",
        "user_id":       str(user.id),
        "role":          user.role.value,
    }, message="Login successful")


# ── /auth/me — CCO + Admin unified profile endpoint ───────────────────────────
@router.get("/me", summary="Get my full profile [CCO/Admin/SuperAdmin]")
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the authenticated user's full profile.
    Works for CCO, ADMIN, SUPER_ADMIN roles.
    Returns all available fields from the User model.
    """
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(current_user["user_id"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return success_response(data={
        "id":                str(user.id),
        "name":              user.name,
        "mobile":            user.mobile,
        "email":             user.email,
        "role":              user.role.value,
        "city":              user.city,
        "profile_image":     user.profile_image,
        "id_proof_url":      user.id_proof_url,
        "id_proof_type":     user.id_proof_type,
        "address_proof_url": user.address_proof_url,
        "address_proof_type":user.address_proof_type,
        "is_verified":       user.is_verified,
        "is_active":         user.is_active,
        "created_at":        user.created_at.isoformat(),
        "updated_at":        user.updated_at.isoformat() if user.updated_at else None,
    })


# ── PUT /auth/me — update own profile [CCO/Admin] ─────────────────────────────
class UpdateMeRequest(PydanticBaseModel):
    name:  Optional[str] = None
    email: Optional[str] = None
    city:  Optional[str] = None

@router.put("/me", summary="Update my profile [CCO/Admin/SuperAdmin]")
async def update_me(
    payload: UpdateMeRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(current_user["user_id"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.name:  user.name  = payload.name
    if payload.email: user.email = payload.email
    if payload.city:  user.city  = payload.city
    await db.commit()
    return success_response(message="Profile updated successfully")
