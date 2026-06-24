from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreateRefundRequest(BaseModel):
    booking_id: str; payment_id: Optional[str] = None; amount: float
    reason: str; refund_method: str = "ORIGINAL"

class ProcessRefundRequest(BaseModel):
    notes: Optional[str] = None; gateway_refund_id: Optional[str] = None

@router.post("", summary="Initiate refund request")
async def create_refund(payload: CreateRefundRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.refund import Refund
    r = Refund(booking_id=UUID(payload.booking_id), amount=payload.amount, reason=payload.reason,
               refund_method=payload.refund_method,
               payment_id=UUID(payload.payment_id) if payload.payment_id else None)
    db.add(r); await db.commit()
    return success_response(data={"id": str(r.id), "status": r.status}, message="Refund request created")

@router.get("", summary="List refunds [Admin]")
async def list_refunds(page: int = Query(1, ge=1), per_page: int = Query(20), status: Optional[str] = None,
                       current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.refund import Refund
    q = select(Refund)
    if status: q = q.where(Refund.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(Refund.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(r.id), "booking_id": str(r.booking_id),
                                              "amount": r.amount, "reason": r.reason, "status": r.status,
                                              "refund_method": r.refund_method,
                                              "created_at": r.created_at.isoformat()} for r in items], "total": total})

@router.post("/{refund_id}/approve", summary="Approve refund [Admin]")
async def approve_refund(refund_id: UUID, payload: ProcessRefundRequest,
                         current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.refund import Refund
    from datetime import datetime, timezone
    r = (await db.execute(select(Refund).where(Refund.id == refund_id))).scalar_one_or_none()
    if not r: raise HTTPException(404, "Refund not found")
    r.status = "APPROVED"; r.processed_by = UUID(current_user["user_id"]); r.notes = payload.notes
    await db.commit()
    return success_response(message="Refund approved")

@router.post("/{refund_id}/process", summary="Mark refund processed [Admin]")
async def process_refund(refund_id: UUID, payload: ProcessRefundRequest,
                         current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.refund import Refund
    from datetime import datetime, timezone
    r = (await db.execute(select(Refund).where(Refund.id == refund_id))).scalar_one_or_none()
    if not r: raise HTTPException(404, "Refund not found")
    r.status = "PROCESSED"; r.gateway_refund_id = payload.gateway_refund_id
    r.processed_at = datetime.now(timezone.utc); r.notes = payload.notes
    await db.commit()
    return success_response(message="Refund processed")
