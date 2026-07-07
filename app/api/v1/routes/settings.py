"""
System Settings — /api/v1/settings

GET  /settings/general              Get general settings [Staff]
PUT  /settings/general              Update general settings [Admin]
GET  /settings/payment              Payment config [Admin]
PUT  /settings/payment              Update payment [Admin]
GET  /settings/notification         Notification config [Admin]
PUT  /settings/notification         Update notification [Admin]
GET  /settings/security             Security config [Admin]
PUT  /settings/security             Update security [Admin]
GET  /settings/cloudinary           Cloudinary config [Admin]
PUT  /settings/cloudinary           Update Cloudinary config [Admin]
GET  /settings/group/{group}        Get all keys in a group [Admin]
PUT  /settings/group/{group}        Upsert multiple keys [Admin]
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, Any, Dict
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff, AdminOrTech, AdminOrCCO, get_current_user
from app.models.system_setting import SystemSetting
from app.utils.response import success_response
from app.core.security import hash_password, verify_password

router = APIRouter()

# ── helpers ──────────────────────────────────────────────────────────────────

CLOUDINARY_KEYS = [
    ("cloud_name",     "Cloud Name",      False),
    ("api_key",        "API Key",         False),
    ("api_secret",     "API Secret",      True),
    ("upload_preset",  "Upload Preset",   False),
    ("folder",         "Default Folder",  False),
]
PLATFORM_KEYS = [
    ("app_name",      "Platform Name",    False),
    ("tagline",       "Tagline",          False),
    ("logo_url",      "Logo URL",         False),
    ("favicon_url",   "Favicon URL",      False),
    ("primary_color", "Primary Color",    False),
    ("support_email", "Support Email",    False),
    ("support_phone", "Support Phone",    False),
    ("address",       "Business Address", False),
    ("website_url",   "Website URL",      False),
    ("gst_number",    "GST Number",       False),
    ("currency",      "Currency",         False),
    ("timezone",      "Timezone",         False),
]

GENERAL_KEYS = [
    ("app_name",        "App Name",         False),
    ("support_phone",   "Support Phone",    False),
    ("support_email",   "Support Email",    False),
    ("business_address","Business Address", False),
    ("gst_number",      "GST Number",       False),
    ("invoice_prefix",  "Invoice Prefix",   False),
    ("currency",        "Currency",         False),
    ("timezone",        "Timezone",         False),
]

PAYMENT_KEYS = [
    ("razorpay_key_id",     "Razorpay Key ID",     False),
    ("razorpay_key_secret", "Razorpay Key Secret", True),
    ("payment_gateway",     "Payment Gateway",     False),
    ("upi_enabled",         "UPI Enabled",         False),
    ("cash_enabled",        "Cash Enabled",        False),
]

NOTIFICATION_KEYS = [
    ("sms_api_key",       "SMS API Key",        True),
    ("sms_sender_id",     "SMS Sender ID",      False),
    ("whatsapp_api_key",  "WhatsApp API Key",   True),
    ("whatsapp_phone_id", "WhatsApp Phone ID",  False),
    ("email_host",        "Email Host",         False),
    ("email_port",        "Email Port",         False),
    ("email_username",    "Email Username",     False),
    ("email_password",    "Email Password",     True),
    ("from_email",        "From Email",         False),
    ("from_name",         "From Name",          False),
]

SECURITY_KEYS = [
    ("otp_expiry_minutes",   "OTP Expiry (minutes)",    False),
    ("jwt_expiry_minutes",   "JWT Expiry (minutes)",    False),
    ("max_login_attempts",   "Max Login Attempts",      False),
    ("refresh_token_days",   "Refresh Token (days)",    False),
]

MAPS_KEYS = [
    ("google_maps_api_key",    "Google Maps API Key",       True),
    ("geocoding_enabled",      "Geocoding Enabled",         False),
    ("geofence_radius_meters", "Geofence Radius (meters)",  False),
    ("assignment_radius_km",   "Assignment Radius (km)",    False),
]

DISPATCH_KEYS = [
    ("auto_assign_enabled",       "Auto-assign Enabled",              False),
    ("response_timeout_minutes",  "Response Timeout (minutes)",       False),
    ("max_reject_before_penalty", "Max Rejects Before Penalty",       False),
    ("max_active_bookings",       "Max Active Bookings per Tech",     False),
    ("fcm_server_key",            "FCM Server Key (Legacy HTTP v1)",  True),
]

FIREBASE_KEYS = [
    ("firebase_project_id",    "Project ID",          False),
    ("firebase_client_email",  "Client Email",        False),
    ("firebase_private_key",   "Private Key (PEM)",   True),
    ("firebase_sdk_json",      "Full SDK JSON",        True),
]

GROUP_KEYS: Dict[str, list] = {
    "general":      GENERAL_KEYS,
    "payment":      PAYMENT_KEYS,
    "notification": NOTIFICATION_KEYS,
    "security":     SECURITY_KEYS,
    "cloudinary":   CLOUDINARY_KEYS,
    "platform":     PLATFORM_KEYS,
    "maps":         MAPS_KEYS,
    "dispatch":     DISPATCH_KEYS,
    "firebase":     FIREBASE_KEYS,
}

GROUP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "general": {
        "app_name": "Palei Solutions", "support_phone": "+91-XXXXXXXXXX",
        "support_email": "support@palei.in", "business_address": "Bhubaneswar, Odisha, India",
        "gst_number": "", "invoice_prefix": "PAL", "currency": "INR", "timezone": "Asia/Kolkata",
    },
    "payment": {
        "razorpay_key_id": "", "razorpay_key_secret": "",
        "payment_gateway": "razorpay", "upi_enabled": "true", "cash_enabled": "true",
    },
    "notification": {
        "sms_api_key": "", "sms_sender_id": "", "whatsapp_api_key": "",
        "whatsapp_phone_id": "", "email_host": "smtp.gmail.com", "email_port": "587",
        "email_username": "", "email_password": "", "from_email": "", "from_name": "Palei Solutions",
    },
    "security": {
        "otp_expiry_minutes": "10", "jwt_expiry_minutes": "30",
        "max_login_attempts": "5", "refresh_token_days": "30",
    },
    "cloudinary": {
        "cloud_name": "", "api_key": "", "api_secret": "",
        "upload_preset": "", "folder": "palei",
    },
    "platform": {
        "app_name": "Palei Solutions", "tagline": "Home Services Platform",
        "logo_url": "", "favicon_url": "", "primary_color": "#1B4FD8",
        "support_email": "support@palei.in", "support_phone": "",
        "address": "", "website_url": "", "gst_number": "",
        "currency": "INR", "timezone": "Asia/Kolkata",
    },
    "maps": {
        "google_maps_api_key": "",
        "geocoding_enabled": "true",
        "geofence_radius_meters": "100",
        "assignment_radius_km": "20",
    },
    "dispatch": {
        "auto_assign_enabled": "true",
        "response_timeout_minutes": "5",
        "max_reject_before_penalty": "3",
        "max_active_bookings": "5",
        "fcm_server_key": "",
    },
    "firebase": {
        "firebase_project_id": "",
        "firebase_client_email": "",
        "firebase_private_key": "",
        "firebase_sdk_json": "",
    },
}


async def _get_group(db: AsyncSession, group: str) -> Dict[str, Any]:
    """Return all settings for a group as a flat dict, falling back to defaults."""
    rows = (await db.execute(
        select(SystemSetting).where(SystemSetting.group == group)
    )).scalars().all()

    defaults = GROUP_DEFAULTS.get(group, {})
    result = dict(defaults)
    for row in rows:
        # Mask secrets — return "***" if is_secret and value is set
        if row.is_secret and row.value:
            result[row.key] = "***"
        else:
            result[row.key] = row.value or defaults.get(row.key, "")
    return result


async def _upsert_group(db: AsyncSession, group: str, data: Dict[str, Any]) -> None:
    """Upsert a dict of key→value into system_settings for the given group."""
    key_meta = {k: (label, secret) for k, label, secret in GROUP_KEYS.get(group, [])}
    for key, value in data.items():
        if value is None:
            continue
        # Skip masked values — don't overwrite real secret with "***"
        if str(value) == "***":
            continue
        existing = (await db.execute(
            select(SystemSetting).where(
                SystemSetting.group == group,
                SystemSetting.key == key,
            )
        )).scalar_one_or_none()
        label, is_secret = key_meta.get(key, (key, False))
        if existing:
            existing.value = str(value)
        else:
            db.add(SystemSetting(group=group, key=key, value=str(value),
                                 label=label, is_secret=is_secret))
    await db.commit()


# ── Generic group endpoints ───────────────────────────────────────────────────

@router.get("/group/{group}", summary="Get settings group [Admin]")
async def get_group(group: str, current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, group))


@router.put("/group/{group}", summary="Update settings group [Admin]")
async def update_group(group: str, payload: Dict[str, Any],
                       current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, group, payload)
    return success_response(message=f"{group.title()} settings updated")


# ── Named endpoints (keep backward compat) ────────────────────────────────────

@router.get("/general", summary="Get general settings [Staff]")
async def get_general(current_user=Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "general"))


@router.put("/general", summary="Update general settings [Admin]")
async def update_general(payload: Dict[str, Any],
                         current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "general", payload)
    return success_response(message="General settings updated")


@router.get("/payment", summary="Payment settings [Admin]")
async def get_payment(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "payment"))


@router.put("/payment", summary="Update payment settings [Admin]")
async def update_payment(payload: Dict[str, Any],
                         current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "payment", payload)
    return success_response(message="Payment settings updated")


@router.get("/notification", summary="Notification settings [Admin]")
async def get_notification(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "notification"))


@router.put("/notification", summary="Update notification settings [Admin]")
async def update_notification(payload: Dict[str, Any],
                              current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "notification", payload)
    return success_response(message="Notification settings updated")


@router.get("/security", summary="Security settings [Admin]")
async def get_security(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "security"))


@router.put("/security", summary="Update security settings [Admin]")
async def update_security(payload: Dict[str, Any],
                          current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "security", payload)
    return success_response(message="Security settings updated")


@router.get("/cloudinary", summary="Cloudinary settings [Admin/Tech]")
async def get_cloudinary(current_user=Depends(AdminOrTech), db: AsyncSession = Depends(get_db)):
    # Return only upload-safe fields to non-admin callers (no api_secret)
    data = await _get_group(db, "cloudinary")
    role = current_user.get("role", "")
    if role not in ("SUPER_ADMIN", "ADMIN"):
        data = {k: v for k, v in data.items() if k in ("cloud_name", "upload_preset", "folder")}
    return success_response(data=data)


@router.put("/cloudinary", summary="Update Cloudinary settings [Admin]")
async def update_cloudinary(payload: Dict[str, Any],
                            current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "cloudinary", payload)
    return success_response(message="Cloudinary settings updated")


@router.delete("/cloudinary/image", summary="Delete a Cloudinary image by public_id [Tech/Admin]")
async def delete_cloudinary_image(
    public_id: str,
    current_user=Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Proxies a Cloudinary delete request using the stored api_key + api_secret.
    The mobile app never receives the api_secret — only the backend signs the request.
    """
    import hashlib, time, httpx
    data = await _get_group(db, "cloudinary")
    cloud_name = data.get("cloud_name", "")
    api_key    = data.get("api_key", "")
    api_secret = data.get("api_secret", "")
    if not cloud_name or not api_key or not api_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Cloudinary credentials not configured in settings.")

    timestamp  = int(time.time())
    # Sign: sha1("public_id=<id>&timestamp=<ts><api_secret>")
    sign_str   = f"public_id={public_id}&timestamp={timestamp}{api_secret}"
    signature  = hashlib.sha1(sign_str.encode()).hexdigest()

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/image/destroy",
            data={
                "public_id": public_id,
                "api_key":   api_key,
                "timestamp": timestamp,
                "signature": signature,
            },
        )
    result = resp.json()
    if result.get("result") not in ("ok", "not found"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Cloudinary delete failed: {result}")
    return success_response(data=result, message="Image deleted from Cloudinary")


# ── Platform branding endpoints ───────────────────────────────────────────────

@router.get("/platform", summary="Get platform branding settings [Staff]")
async def get_platform(current_user=Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "platform"))


@router.put("/platform", summary="Update platform branding [Admin]")
async def update_platform(payload: Dict[str, Any],
                          current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "platform", payload)
    return success_response(message="Platform settings updated")


# ── Maps endpoints ────────────────────────────────────────────────────────────

@router.get("/maps", summary="Maps & Geocoding settings [Admin/Tech]")
async def get_maps(current_user=Depends(AdminOrTech), db: AsyncSession = Depends(get_db)):
    # Return only the API key to technicians (not full admin config)
    data = await _get_group(db, "maps")
    role = current_user.get("role", "")
    if role not in ("SUPER_ADMIN", "ADMIN"):
        data = {k: v for k, v in data.items() if k == "google_maps_api_key"}
    return success_response(data=data)


@router.put("/maps", summary="Update Maps settings [Admin]")
async def update_maps(payload: Dict[str, Any],
                      current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "maps", payload)
    return success_response(message="Maps settings updated")


# ── Dispatch endpoints ─────────────────────────────────────────────────────────

@router.get("/dispatch", summary="Dispatch & Assignment settings [Admin]")
async def get_dispatch(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "dispatch"))


@router.put("/dispatch", summary="Update Dispatch settings [Admin]")
async def update_dispatch(payload: Dict[str, Any],
                          current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "dispatch", payload)
    return success_response(message="Dispatch settings updated")


# ── Firebase endpoints ─────────────────────────────────────────────────────────

@router.get("/firebase", summary="Firebase Admin SDK settings [Admin]")
async def get_firebase(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "firebase"))


@router.put("/firebase", summary="Update Firebase settings [Admin]")
async def update_firebase(payload: Dict[str, Any],
                          current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "firebase", payload)
    return success_response(message="Firebase settings updated")


# ── Platform public ────────────────────────────────────────────────────────────

@router.get("/platform/public", summary="Get platform settings (public — no auth)")
async def get_platform_public(db: AsyncSession = Depends(get_db)):
    """Public endpoint — used by the admin dashboard login page and initial load."""
    data = await _get_group(db, "platform")
    return success_response(data=data)


@router.get("/profile-complete", summary="Check if platform profile is set up [Staff]")
async def profile_complete(current_user=Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    """Returns whether the minimum required platform settings are filled."""
    data = await _get_group(db, "platform")
    required = ["app_name", "support_email"]
    missing = [k for k in required if not data.get(k)]
    return success_response(data={
        "complete": len(missing) == 0,
        "missing": missing,
        "settings": data,
    })


# ── Maps key public ────────────────────────────────────────────────────────────
@router.get("/maps/public", summary="Get Google Maps API key (public — no auth)")
async def get_maps_public(db: AsyncSession = Depends(get_db)):
    """Public endpoint — used by website tracking page to load Google Maps."""
    data = await _get_group(db, "maps")
    # Only expose the API key, nothing else
    return success_response(data={"google_maps_api_key": data.get("google_maps_api_key", "")})


# ── Payment public ────────────────────────────────────────────────────────────
@router.get("/payment/public", summary="Get payment gateway availability (public — no auth)")
async def get_payment_public(db: AsyncSession = Depends(get_db)):
    """
    Public endpoint — apps use this to decide whether to show the "Pay Now"
    button at all. Exposes ONLY the Razorpay key id (safe/required for the
    client-side Checkout SDK) and enabled flags — never the key secret.
    """
    rows = (await db.execute(
        select(SystemSetting).where(SystemSetting.group == "payment")
    )).scalars().all()
    values = {row.key: row.value for row in rows}
    key_id = values.get("razorpay_key_id") or ""
    key_secret = values.get("razorpay_key_secret") or ""
    return success_response(data={
        "razorpay_enabled": bool(key_id and key_secret),
        "razorpay_key_id": key_id,
        "upi_enabled": (values.get("upi_enabled", "true") == "true"),
        "cash_enabled": (values.get("cash_enabled", "true") == "true"),
    })


# ── MPIN — Dashboard Auto-Lock ────────────────────────────────────────────────
#
# Stored directly via SystemSetting (group="mpin") rather than through the
# generic _get_group()/_upsert_group() helpers, because the hash must NEVER
# be sent to the client — not even masked as "***" — and verification needs
# a bcrypt compare against the real stored hash, not a string-equality PUT.
#
# Keys used: group="mpin"
#   enabled    -> "true" | "false"
#   mpin_hash  -> bcrypt hash of the 6-digit PIN (never returned to client)

class MpinSetBody(BaseModel):
    mpin: str


class MpinVerifyBody(BaseModel):
    mpin: str


class MpinEnableBody(BaseModel):
    enabled: bool


async def _get_mpin_setting(db: AsyncSession, key: str) -> Optional[str]:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.group == "mpin", SystemSetting.key == key)
    )).scalar_one_or_none()
    return row.value if row else None


async def _set_mpin_setting(db: AsyncSession, key: str, value: str, is_secret: bool = False) -> None:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.group == "mpin", SystemSetting.key == key)
    )).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(group="mpin", key=key, value=value,
                             label=key, is_secret=is_secret))
    await db.commit()


@router.get("/mpin/status", summary="Get MPIN lock status [Admin]")
async def get_mpin_status(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    """
    Returns whether the 10-minute MPIN auto-lock is enabled and whether a
    PIN has already been configured. Never returns the PIN or its hash.
    """
    enabled_raw = await _get_mpin_setting(db, "enabled")
    pin_hash = await _get_mpin_setting(db, "mpin_hash")
    return success_response(data={
        "enabled": enabled_raw == "true",
        "configured": bool(pin_hash),
    })


@router.post("/mpin/set", summary="Set / change the 6-digit MPIN [Admin]")
async def set_mpin(body: MpinSetBody, current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    pin = (body.mpin or "").strip()
    if not pin.isdigit() or len(pin) != 6:
        raise HTTPException(status_code=400, detail="MPIN must be exactly 6 digits")
    await _set_mpin_setting(db, "mpin_hash", hash_password(pin), is_secret=True)
    return success_response(message="MPIN saved successfully")


@router.post("/mpin/enable", summary="Enable / disable the MPIN auto-lock [Admin]")
async def enable_mpin(body: MpinEnableBody, current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    if body.enabled:
        pin_hash = await _get_mpin_setting(db, "mpin_hash")
        if not pin_hash:
            raise HTTPException(status_code=400, detail="Set an MPIN before enabling auto-lock")
    await _set_mpin_setting(db, "enabled", "true" if body.enabled else "false")
    return success_response(message=f"MPIN auto-lock {'enabled' if body.enabled else 'disabled'}")


@router.post("/mpin/verify", summary="Verify MPIN to unlock the dashboard [Admin]")
async def verify_mpin(body: MpinVerifyBody, current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    pin_hash = await _get_mpin_setting(db, "mpin_hash")
    if not pin_hash:
        return success_response(data={"valid": False}, message="No MPIN configured")
    valid = verify_password((body.mpin or "").strip(), pin_hash)
    return success_response(data={"valid": valid})


# ── CCO MPIN — Per-user MPIN (separate from admin dashboard MPIN) ─────────────
# CCOs have their own per-user MPIN stored in SystemSetting with
# group="cco_mpin" and key=user_id. This is separate from the admin dashboard
# auto-lock MPIN which is shared and stored with group="mpin".

class CcoMpinSetBody(BaseModel):
    mpin: str

class CcoMpinVerifyBody(BaseModel):
    mpin: str


@router.get("/cco/mpin/status", summary="CCO: check if MPIN is configured")
async def cco_mpin_status(
    current_user=Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    # Check per-user cco_mpin group first, then fall back to legacy shared mpin group
    pin_hash = await _get_mpin_setting_user(db, user_id)
    if not pin_hash:
        pin_hash = await _get_mpin_setting(db, "mpin_hash")
    return success_response(data={"configured": bool(pin_hash)})


@router.post("/cco/mpin/set", summary="CCO: set or change own MPIN")
async def cco_mpin_set(
    body: CcoMpinSetBody,
    current_user=Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    pin = (body.mpin or "").strip()
    if not pin.isdigit() or len(pin) != 6:
        raise HTTPException(status_code=400, detail="MPIN must be exactly 6 digits")
    user_id = current_user["user_id"]
    from app.core.security import hash_password as _hash
    hashed = _hash(pin)
    row = (await db.execute(
        select(SystemSetting).where(
            SystemSetting.group == "cco_mpin", SystemSetting.key == user_id
        )
    )).scalar_one_or_none()
    if row:
        row.value = hashed
    else:
        db.add(SystemSetting(group="cco_mpin", key=user_id, value=hashed,
                             label="CCO MPIN", is_secret=True))
    await db.commit()
    return success_response(message="MPIN saved successfully")


@router.post("/cco/mpin/verify", summary="CCO: verify MPIN")
async def cco_mpin_verify(
    body: CcoMpinVerifyBody,
    current_user=Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    # Check per-user store first, then fall back to legacy shared mpin
    pin_hash = await _get_mpin_setting_user(db, user_id)
    if not pin_hash:
        pin_hash = await _get_mpin_setting(db, "mpin_hash")
    if not pin_hash:
        return success_response(data={"valid": False}, message="No MPIN configured")
    from app.core.security import verify_password as _verify
    valid = _verify((body.mpin or "").strip(), pin_hash)
    return success_response(data={"valid": valid})


async def _get_mpin_setting_user(db: AsyncSession, user_id: str):
    """Get per-user MPIN hash from cco_mpin group."""
    row = (await db.execute(
        select(SystemSetting).where(
            SystemSetting.group == "cco_mpin", SystemSetting.key == user_id
        )
    )).scalar_one_or_none()
    return row.value if row else None
