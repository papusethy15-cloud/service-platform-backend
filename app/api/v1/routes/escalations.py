from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreateEscalationRequest(BaseModel):
    booking_id: Optional[str] = None; subject: str; description: str; priority: str = "MEDIUM"

class EscalationActionRequest(BaseModel):
    escalation_id: str; notes: Optional[str] = None; assigned_to: Optional[str] = None

@router.post("", summary="Create complaint/escalation")
async def create_escalation(payload: CreateEscalationRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation, EscalationStatus
    esc = Escalation(created_by=UUID(current_user["user_id"]), booking_id=UUID(payload.booking_id) if payload.booking_id else None, subject=payload.subject, description=payload.description, priority=payload.priority, status=EscalationStatus.OPEN)
    db.add(esc); await db.commit()
    return success_response(data={"id": str(esc.id), "status": esc.status.value}, message="Complaint submitted")

@router.get("", summary="List escalations [Admin/CCO]")
async def list_escalations(page: int = Query(1, ge=1), per_page: int = Query(20), status: str = Query(None), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation, EscalationStatus
    q = select(Escalation).where(Escalation.is_active == True)
    if status: q = q.where(Escalation.status == EscalationStatus(status))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(Escalation.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(e.id), "subject": e.subject, "priority": e.priority, "status": e.status.value, "created_at": e.created_at.isoformat()} for e in items], "total": total})

@router.get("/{escalation_id}", summary="Escalation details")
async def get_escalation(escalation_id: UUID, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation
    esc = (await db.execute(select(Escalation).where(Escalation.id == escalation_id, Escalation.is_active == True))).scalar_one_or_none()
    if not esc: raise HTTPException(status_code=404, detail="Escalation not found")
    return success_response(data={"id": str(esc.id), "subject": esc.subject, "description": esc.description, "priority": esc.priority, "status": esc.status.value, "assigned_to": str(esc.assigned_to) if esc.assigned_to else None, "created_at": esc.created_at.isoformat()})

@router.post("/assign", summary="Assign escalation [Admin/CCO]")
async def assign_escalation(payload: EscalationActionRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc: raise HTTPException(status_code=404, detail="Escalation not found")
    esc.assigned_to = UUID(payload.assigned_to) if payload.assigned_to else None
    esc.status = EscalationStatus.IN_PROGRESS
    await db.commit()
    return success_response(message="Escalation assigned")

@router.post("/resolve", summary="Resolve escalation [Admin/CCO]")
async def resolve_escalation(payload: EscalationActionRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc: raise HTTPException(status_code=404, detail="Escalation not found")
    esc.status = EscalationStatus.RESOLVED; esc.resolved_by = UUID(current_user["user_id"]); esc.resolved_at = datetime.utcnow(); esc.resolution_notes = payload.notes
    await db.commit()
    return success_response(message="Escalation resolved")

@router.post("/escalate", summary="Escalate complaint to higher authority [Admin/CCO]")
async def escalate(payload: EscalationActionRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc: raise HTTPException(status_code=404, detail="Escalation not found")
    esc.status = EscalationStatus.ESCALATED; esc.escalation_level = (esc.escalation_level or 1) + 1; esc.escalation_notes = payload.notes
    await db.commit()
    return success_response(message="Escalated to higher authority")

@router.get("/history", summary="Escalation history [Admin/CCO]")
async def escalation_history(page: int = Query(1, ge=1), per_page: int = Query(20), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.escalation import Escalation
    q = select(Escalation).order_by(Escalation.created_at.desc())
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(e.id), "subject": e.subject, "status": e.status.value, "priority": e.priority, "created_at": e.created_at.isoformat()} for e in items], "total": total})
