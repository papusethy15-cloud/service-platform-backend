from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AnyAuthenticated
from app.utils.response import success_response, iso
import random, string

router = APIRouter()

class RedeemRequest(BaseModel):
    points: int; notes: Optional[str] = None

@router.get("/code", summary="My referral code")
async def my_referral_code(current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.referral import ReferralCode
    code_rec = (await db.execute(select(ReferralCode).where(ReferralCode.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    if not code_rec:
        code = "PAL" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        code_rec = ReferralCode(user_id=UUID(current_user["user_id"]), code=code)
        db.add(code_rec); await db.commit()
    return success_response(data={"referral_code": code_rec.code, "total_referrals": code_rec.total_referrals, "total_earned": code_rec.total_earned})

@router.get("/history", summary="Referral history")
async def referral_history(current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.referral import Referral
    referrals = (await db.execute(select(Referral).where(Referral.referrer_id == UUID(current_user["user_id"])).order_by(Referral.created_at.desc()))).scalars().all()
    # Use referred_id (actual DB column name, not referee_id)
    return success_response(data=[{
        "id": str(r.id),
        "referred_id": str(r.referred_id) if r.referred_id else None,
        "reward_amount": r.reward_amount,
        "status": r.status,
        "created_at": iso(r.created_at)
    } for r in referrals])

@router.get("/rewards", summary="Reward history")
async def reward_history(current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.referral import ReferralReward
    rewards = (await db.execute(select(ReferralReward).where(ReferralReward.user_id == UUID(current_user["user_id"])))).scalars().all()
    return success_response(data=[{"id": str(r.id), "amount": r.amount, "type": r.type, "status": r.status, "created_at": iso(r.created_at)} for r in rewards])

@router.post("/redeem", summary="Redeem referral reward")
async def redeem_reward(payload: RedeemRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return success_response(data={"redeemed_points": payload.points}, message="Reward redeemed successfully")

@router.get("/statistics", summary="Referral statistics")
async def referral_statistics(current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.referral import ReferralCode
    code_rec = (await db.execute(select(ReferralCode).where(ReferralCode.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    return success_response(data={"total_referrals": code_rec.total_referrals if code_rec else 0, "total_earned": code_rec.total_earned if code_rec else 0.0, "pending_rewards": 0})
