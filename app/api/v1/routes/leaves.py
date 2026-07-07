from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from datetime import date
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreateLeaveRequest(BaseModel):
    leave_type: str; from_date: date; to_date: date; reason: str

class ReviewLeaveRequest(BaseModel):
    status: str; notes: Optional[str] = None

@router.post("", summary="Apply for leave")
async def apply_leave(payload: CreateLeaveRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import LeaveRequest
    from app.models.technician import Technician
    tech = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    if not tech: raise HTTPException(403, "Only technicians can apply for leave")
    leave = LeaveRequest(technician_id=tech.id, **payload.dict())
    db.add(leave); await db.commit()
    return success_response(data={"id": str(leave.id)}, message="Leave request submitted")

@router.get("/me", summary="My leave requests [Technician]")
async def my_leaves(page: int = Query(1, ge=1), per_page: int = Query(20),
                    current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import LeaveRequest
    from app.models.technician import Technician
    tech = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    if not tech: raise HTTPException(403, "Only technicians can view their leave requests")
    q = select(LeaveRequest).where(LeaveRequest.technician_id == tech.id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(LeaveRequest.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(l.id), "technician_id": str(l.technician_id),
                                              "leave_type": l.leave_type, "from_date": str(l.from_date),
                                              "to_date": str(l.to_date), "reason": l.reason,
                                              "status": l.status} for l in items], "total": total})


@router.get("", summary="Leave requests [Admin/CCO]")
async def list_leaves(page: int = Query(1, ge=1), per_page: int = Query(20), status: Optional[str] = None,
                      current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import LeaveRequest
    q = select(LeaveRequest)
    if status: q = q.where(LeaveRequest.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(LeaveRequest.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(l.id), "technician_id": str(l.technician_id),
                                              "leave_type": l.leave_type, "from_date": str(l.from_date),
                                              "to_date": str(l.to_date), "reason": l.reason,
                                              "status": l.status} for l in items], "total": total})

@router.post("/{leave_id}/review", summary="Approve/Reject leave [Admin]")
async def review_leave(leave_id: UUID, payload: ReviewLeaveRequest,
                       current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import LeaveRequest
    from datetime import datetime, timezone
    leave = (await db.execute(select(LeaveRequest).where(LeaveRequest.id == leave_id))).scalar_one_or_none()
    if not leave: raise HTTPException(404, "Leave request not found")
    if payload.status not in ["APPROVED", "REJECTED"]: raise HTTPException(400, "Invalid status")
    leave.status = payload.status
    leave.approved_by = UUID(current_user["user_id"]); leave.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return success_response(message=f"Leave {payload.status.lower()}")
