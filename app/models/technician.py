from sqlalchemy import Column, String, Text, Float, Boolean, Integer, ForeignKey, Date, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.models.base import BaseModel


class TechnicianStatus(str, enum.Enum):
    ACTIVE    = "ACTIVE"
    INACTIVE  = "INACTIVE"
    ON_LEAVE  = "ON_LEAVE"
    SUSPENDED = "SUSPENDED"


class Technician(BaseModel):
    __tablename__ = "technicians"

    user_id          = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)

    # Core identity
    name             = Column(String(150), nullable=False)
    mobile           = Column(String(20), nullable=False)
    email            = Column(String(200), nullable=True)
    alternate_mobile = Column(String(20), nullable=True)
    technician_code  = Column(String(30), unique=True, nullable=True)

    # Personal
    dob              = Column(Date, nullable=True)
    gender           = Column(String(10), nullable=True)    # MALE | FEMALE | OTHER

    # Location
    city             = Column(String(100), nullable=True)
    area             = Column(String(200), nullable=True)
    pincode          = Column(String(10), nullable=True)
    address          = Column(Text, nullable=True)

    # Professional
    experience_years = Column(Integer, default=0)
    status           = Column(SAEnum(TechnicianStatus), default=TechnicianStatus.ACTIVE)
    rating           = Column(Float, default=0.0)
    total_jobs       = Column(Integer, default=0)

    # Documents / Images
    profile_image    = Column(String(500), nullable=True)
    id_proof         = Column(String(500), nullable=True)
    identity_type    = Column(String(50), nullable=True)    # Aadhaar, PAN, etc.
    identity_number  = Column(String(50), nullable=True)

    # Emergency contact
    emergency_contact_name   = Column(String(150), nullable=True)
    emergency_contact_mobile = Column(String(20), nullable=True)


class TechnicianSkill(BaseModel):
    __tablename__ = "technician_skills"
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    service_id    = Column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=False)
    proficiency   = Column(String(20), default="INTERMEDIATE")  # BEGINNER | INTERMEDIATE | EXPERT


class TechnicianRating(BaseModel):
    __tablename__ = "technician_ratings"
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    booking_id    = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    customer_id   = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    rating        = Column(Float, nullable=False)
    review        = Column(Text, nullable=True)


class TechnicianAvailability(BaseModel):
    __tablename__ = "technician_availability"
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    day_of_week   = Column(Integer, nullable=False)    # 0=Mon … 6=Sun
    start_time    = Column(String(8), nullable=False)  # "09:00:00"
    end_time      = Column(String(8), nullable=False)  # "18:00:00"
    is_available  = Column(Boolean, default=True)
