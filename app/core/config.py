from pydantic_settings import BaseSettings
from typing import List
import os

# Absolute path to .env — works regardless of PM2/uvicorn working directory
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")

class Settings(BaseSettings):
    APP_NAME: str = "Palei Solutions"
    APP_ENV: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:3001", "http://localhost:5173", "http://127.0.0.1:3000", "http://127.0.0.1:3001", "https://bibekenterprises.com", "https://www.bibekenterprises.com", "https://admin.bibekenterprises.com", "https://api.bibekenterprises.com"]

    DATABASE_URL: str = "postgresql://palei_user:palei_pass@localhost:5433/palei_solutions"
    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = "dev-jwt-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15    # Short-lived; clients must use refresh_token to get a new one (rolling 30-day session)
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    OTP_EXPIRE_MINUTES: int = 10
    OTP_LENGTH: int = 6

    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_NAME: str = "palei-storage"

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    SMS_API_KEY: str = ""
    WHATSAPP_API_KEY: str = ""

    SENTRY_DSN: str = ""

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

settings = Settings()
