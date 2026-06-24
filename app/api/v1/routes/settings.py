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
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, Any, Dict
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff
from app.models.system_setting import SystemSetting
from app.utils.response import success_response

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

GROUP_KEYS: Dict[str, list] = {
    "general":      GENERAL_KEYS,
    "payment":      PAYMENT_KEYS,
    "notification": NOTIFICATION_KEYS,
    "security":     SECURITY_KEYS,
    "cloudinary":   CLOUDINARY_KEYS,
    "platform":     PLATFORM_KEYS,
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


@router.get("/cloudinary", summary="Cloudinary settings [Admin]")
async def get_cloudinary(current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "cloudinary"))


@router.put("/cloudinary", summary="Update Cloudinary settings [Admin]")
async def update_cloudinary(payload: Dict[str, Any],
                            current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "cloudinary", payload)
    return success_response(message="Cloudinary settings updated")


# ── Platform branding endpoints ───────────────────────────────────────────────

@router.get("/platform", summary="Get platform branding settings [Staff]")
async def get_platform(current_user=Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    return success_response(data=await _get_group(db, "platform"))


@router.put("/platform", summary="Update platform branding [Admin]")
async def update_platform(payload: Dict[str, Any],
                          current_user=Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _upsert_group(db, "platform", payload)
    return success_response(message="Platform settings updated")


@router.get("/platform/public", summary="Get platform settings (public — no auth)")
async def get_platform_public(db: AsyncSession = Depends(get_db)):
    """Public endpoint — used by the admin dashboard login page and initial load."""
    data = await _get_group(db, "platform")
    return success_response(data=data)


@router.get("/profile-complete", summary="Check if platform profile is set up [Staff]")
async def profile_complete(current_user=Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    """Returns whether the minimum required platform settings are filled."""
    data = await _get_group(db, "platform")
    required = ["app_name", "support_email", "logo_url"]
    missing = [k for k in required if not data.get(k)]
    return success_response(data={
        "complete": len(missing) == 0,
        "missing": missing,
        "settings": data,
    })
