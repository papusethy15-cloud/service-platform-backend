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
    profile_image = Column(String(500), nullable=True)
    is_verified   = Column(Boolean, default=False)
