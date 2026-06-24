"""
SystemSetting — generic key-value store for platform-level credentials
and configuration that admins manage from the Settings page.

Groups:
  cloudinary   — cloud_name, api_key, api_secret, upload_preset
  razorpay     — key_id, key_secret
  sms          — provider, api_key, sender_id
  whatsapp     — provider, api_key, phone_number_id
  email        — host, port, username, password, from_email, from_name
  general      — app_name, support_phone, support_email, timezone, currency
  security     — otp_expiry, jwt_expiry, max_login_attempts
"""
from sqlalchemy import Column, String, Text, Boolean, UniqueConstraint
from app.models.base import BaseModel


class SystemSetting(BaseModel):
    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("group", "key", name="uq_setting_group_key"),)

    group     = Column(String(50),  nullable=False)   # e.g. "cloudinary"
    key       = Column(String(100), nullable=False)   # e.g. "api_key"
    value     = Column(Text,        nullable=True)    # the actual value
    is_secret = Column(Boolean, default=False)        # mask in GET responses if True
    label     = Column(String(200), nullable=True)    # human-readable label
