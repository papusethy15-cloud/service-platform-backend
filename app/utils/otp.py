import random
import string
from app.core.config import settings

def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=settings.OTP_LENGTH))

def get_otp_redis_key(mobile: str) -> str:
    return f"otp:{mobile}"
