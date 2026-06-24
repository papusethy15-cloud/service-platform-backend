from sqlalchemy import Column, String, Float, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel

class ReferralCode(BaseModel):
    __tablename__   = "referral_codes"
    user_id         = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)
    code            = Column(String(20), unique=True, nullable=False)
    total_referrals = Column(Integer, default=0)
    total_earned    = Column(Float, default=0.0)

class Referral(BaseModel):
    __tablename__ = "referrals"
    referrer_id   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    referee_id    = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reward_amount = Column(Float, default=0.0)
    status        = Column(String(20), default="PENDING")

class ReferralReward(BaseModel):
    __tablename__ = "referral_rewards"
    user_id       = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    referral_id   = Column(UUID(as_uuid=True), ForeignKey("referrals.id"), nullable=True)
    amount        = Column(Float, nullable=False)
    type          = Column(String(30), default="CASH")
    status        = Column(String(20), default="PENDING")
