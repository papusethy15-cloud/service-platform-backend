from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreateWarrantyRequest(BaseModel):
    booking_id: Optional[str] = None; customer_id: str
    warranty_type: str = "SERVICE"; description: str
    expiry_date: str; parts_covered: Optional[str] = None

class WarrantyClaimRequest(BaseModel):
    warranty_id: str; description: str; booking_id: Optional[str] = None

class WarrantyActionRequest(BaseModel):
    claim_id: str; notes: Optional[str] = None

@router.get("", summary="List warranties [Admin/CCO]")
async def list_warranties(page: int = Query(1, ge=1), per_page: int = Query(20),
                           customer_id: UUID = Query(None),
                           current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import Warranty
    q = select(Warranty).where(Warranty.is_active == True)
    if customer_id: q = q.where(Warranty.customer_id == customer_id)
    items = (await db.execute(q.order_by(Warranty.expiry_date).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data=[{"id": str(w.id), "warranty_type": w.warranty_type,
                                    "description": w.description, "expiry_date": w.expiry_date.isoformat(),
                                    "status": w.status} for w in items])

@router.post("", summary="Create warranty [Admin]")
async def create_warranty(payload: CreateWarrantyRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import Warranty
    w = Warranty(customer_id=UUID(payload.customer_id),
                 booking_id=UUID(payload.booking_id) if payload.booking_id else None,
                 warranty_type=payload.warranty_type, description=payload.description,
                 expiry_date=datetime.fromisoformat(payload.expiry_date),
                 parts_covered=payload.parts_covered)
    db.add(w); await db.commit()
    return success_response(data={"id": str(w.id)}, message="Warranty created")

@router.get("/{warranty_id}", summary="Warranty details [Staff]")
async def get_warranty(warranty_id: UUID, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import Warranty
    w = (await db.execute(select(Warranty).where(Warranty.id == warranty_id))).scalar_one_or_none()
    if not w: raise HTTPException(status_code=404, detail="Warranty not found")
    return success_response(data={"id": str(w.id), "warranty_type": w.warranty_type,
                                   "description": w.description, "expiry_date": w.expiry_date.isoformat(),
                                   "parts_covered": w.parts_covered, "status": w.status})

@router.post("/claim", summary="Create warranty claim [Customer/CCO]")
async def create_claim(payload: WarrantyClaimRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import WarrantyClaim, Warranty, WarrantyStatus
    w = (await db.execute(select(Warranty).where(Warranty.id == UUID(payload.warranty_id)))).scalar_one_or_none()
    if not w: raise HTTPException(status_code=404, detail="Warranty not found")
    if w.expiry_date < datetime.utcnow(): raise HTTPException(status_code=400, detail="Warranty has expired")
    claim = WarrantyClaim(warranty_id=UUID(payload.warranty_id), claimed_by=UUID(current_user["user_id"]),
                          description=payload.description,
                          booking_id=UUID(payload.booking_id) if payload.booking_id else None)
    db.add(claim); await db.commit()
    return success_response(data={"id": str(claim.id)}, message="Warranty claim submitted")

@router.post("/approve", summary="Approve warranty claim [Admin/CCO]")
async def approve_claim(payload: WarrantyActionRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import WarrantyClaim, ClaimStatus
    claim = (await db.execute(select(WarrantyClaim).where(WarrantyClaim.id == UUID(payload.claim_id)))).scalar_one_or_none()
    if not claim: raise HTTPException(status_code=404, detail="Claim not found")
    claim.status = ClaimStatus.APPROVED; claim.approved_by = UUID(current_user["user_id"]); claim.notes = payload.notes
    await db.commit()
    return success_response(message="Claim approved")

@router.post("/reject", summary="Reject warranty claim [Admin/CCO]")
async def reject_claim(payload: WarrantyActionRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.warranty import WarrantyClaim, ClaimStatus
    claim = (await db.execute(select(WarrantyClaim).where(WarrantyClaim.id == UUID(payload.claim_id)))).scalar_one_or_none()
    if not claim: raise HTTPException(status_code=404, detail="Claim not found")
    claim.status = ClaimStatus.REJECTED; claim.rejected_by = UUID(current_user["user_id"]); claim.notes = payload.notes
    await db.commit()
    return success_response(message="Claim rejected")
