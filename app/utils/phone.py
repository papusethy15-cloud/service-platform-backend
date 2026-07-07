"""
app/utils/phone.py

Canonical mobile-number normalization for the Palei backend.

Root cause this fixes: the website/admin dashboard saved mobile numbers as
bare 10-digit strings ("7894697718") while the customer app (Firebase phone
auth) saved them as full E.164 ("+917894697718"). Every duplicate-check and
lookup in the codebase did a raw string match (User.mobile == payload.mobile),
so the same physical phone number created two completely separate User /
Customer rows depending on which surface the person used to sign up.

Fix: normalize every mobile number to a single canonical form (+91XXXXXXXXXX)
the moment it enters the system, via Pydantic `field_validator(mode="before")`
on every request schema with a `mobile` field. Route code then only ever
sees -- and stores -- the canonical form, so lookups/uniqueness naturally
line up across website, admin dashboard, and the customer/technician apps.
"""
import re
from fastapi import HTTPException


def normalize_mobile(mobile: str | None) -> str | None:
    """Normalize any reasonable Indian mobile number input to +91XXXXXXXXXX.

    Accepts (and normalizes) all of:
      '7894697718', '+917894697718', '917894697718', '+91 78946 97718',
      '091-7894697718', '(91) 7894697718'

    Raises HTTPException(422) if the result isn't a plausible 10-digit
    Indian mobile number, so bad input fails fast at the API boundary
    instead of silently creating a malformed/duplicate-prone record.
    """
    if mobile is None:
        return mobile
    if not isinstance(mobile, str):
        mobile = str(mobile)

    digits = re.sub(r"\D", "", mobile)

    # Drop a leading trunk-prefix '0' some users type (0789... -> 789...,
    # or 091-789... -> 91789...)
    if digits.startswith("0") and len(digits) in (11, 13):
        digits = digits[1:]

    # Drop the country code if present
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    if len(digits) != 10 or digits[0] not in "6789":
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mobile number: '{mobile}'. Expected a 10-digit Indian mobile number.",
        )

    return f"+91{digits}"
