from sqlalchemy import Column, String, Boolean, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum, uuid
from app.models.base import BaseModel

class UserRole(str, enum.Enum):
    SUPER_ADMIN       = "SUPER_ADMIN"
    ADMIN             = "ADMIN"
    CCO               = "CCO"
    TECHNICIAN        = "TECHNICIAN"
    CUSTOMER          = "CUSTOMER"
    ACCOUNTANT        = "ACCOUNTANT"
    INVENTORY_MANAGER = "INVENTORY_MANAGER"

class User(BaseModel):
    __tablename__ = "users"
    name          = Column(String(150), nullable=False)
    mobile        = Column(String(20), unique=True, nullable=False)
    email         = Column(String(200), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=True)
    role          = Column(SAEnum(UserRole), nullable=False, default=UserRole.CUSTOMER)
    city          = Column(String(100), nullable=True)
    profile_image    = Column(String(500), nullable=True)
    id_proof_url      = Column(String(500), nullable=True)   # Cloudinary URL for ID proof doc
    id_proof_type     = Column(String(50),  nullable=True)   # e.g. Aadhaar Card, PAN Card
    address_proof_url = Column(String(500), nullable=True)   # Cloudinary URL for address proof doc
    address_proof_type= Column(String(50),  nullable=True)   # e.g. Utility Bill, Voter ID
    is_verified   = Column(Boolean, default=False)
    fcm_token     = Column(String(500), nullable=True)   # Admin/CCO push notification token
    firebase_uid  = Column(String(128), unique=True, nullable=True)  # Links account to Firebase Auth (Google sign-in)
    mpin_hash     = Column(String(255), nullable=True)  # SHA-256 hash of CCO/Admin MPIN for screen lock
    # CCO payout / salary fields (also used for admin bank details if needed)
    payout_upi_id         = Column(String(200), nullable=True)
    payout_bank_account   = Column(String(100), nullable=True)
    payout_bank_ifsc      = Column(String(20),  nullable=True)
    payout_bank_name      = Column(String(100), nullable=True)
    payout_account_holder = Column(String(150), nullable=True)
    monthly_salary        = Column(Float, nullable=True)
    petrol_amount         = Column(Float, nullable=True)
    mobile_recharge       = Column(Float, nullable=True)
    bonus_amount          = Column(Float, nullable=True)
    hra_amount            = Column(Float, nullable=True)
    other_allowances      = Column(Float, nullable=True)
    salary_notes          = Column(Text, nullable=True)