from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from app.utils.timezone import now_ist, now_naive
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreateEscalationRequest(BaseModel):
    booking_id: Optional[str] = None
    subject: str
    description: str
    priority: str = "MEDIUM"

class EscalationActionRequest(BaseModel):
    escalation_id: str
    notes: Optional[str] = None
    assigned_to: Optional[str] = None

class PatchEscalationRequest(BaseModel):
    status: Optional[str] = None
    resolution_notes: Optional[str] = None
    priority: Optional[str] = None
    assigned_to: Optional[str] = None


def _esc_dict(e, booking=None, customer=None) -> dict:
    """Serialize escalation to a full dict for Admin/CCO consumption."""
    return {
        "id": str(e.id),
        "subject": e.subject,
        "description": e.description,
        "priority": e.priority,
        "status": e.status.value,
        "resolution_notes": e.resolution_notes,
        "escalation_notes": getattr(e, "escalation_notes", None),
        "escalation_level": getattr(e, "escalation_level", 1) or 1,
        "booking_id": str(e.booking_id) if e.booking_id else None,
        "booking_number": booking.booking_number if booking else None,
        "customer_name": (
            getattr(customer, "name", None) or
            f"{getattr(customer, 'first_name', '') or ''} {getattr(customer, 'last_name', '') or ''}".strip()
            if customer else None
        ),
        "customer_mobile": getattr(customer, "mobile", None) or getattr(customer, "mobile_number", None) if customer else None,
        "assigned_to": str(e.assigned_to) if e.assigned_to else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        "created_at": e.created_at.isoformat(),
        "updated_at": e.updated_at.isoformat() if hasattr(e, "updated_at") and e.updated_at else None,
    }


@router.post("", summary="Create complaint/escalation")
async def create_escalation(
    payload: CreateEscalationRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    esc = Escalation(
        created_by=UUID(current_user["user_id"]),
        booking_id=UUID(payload.booking_id) if payload.booking_id else None,
        subject=payload.subject,
        description=payload.description,
        priority=payload.priority,
        status=EscalationStatus.OPEN,
    )
    db.add(esc)
    await db.commit()
    await db.refresh(esc)
    return success_response(data={"id": str(esc.id), "status": esc.status.value}, message="Complaint submitted")


@router.get("", summary="List escalations [Admin/CCO]")
async def list_escalations(
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    limit: int = Query(None),          # alias used by some callers
    status: str = Query(None),
    priority: str = Query(None),
    search: str = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    from app.models.booking import Booking
    from app.models.customer import Customer
    effective_limit = limit if limit is not None else per_page
    q = select(Escalation).where(Escalation.is_active == True)
    if status:
        q = q.where(Escalation.status == EscalationStatus(status))
    if priority:
        q = q.where(Escalation.priority == priority)
    if search:
        q = q.where(Escalation.subject.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(
        q.order_by(Escalation.created_at.desc())
        .offset((page - 1) * effective_limit)
        .limit(effective_limit)
    )).scalars().all()

    # Enrich with booking number + customer info
    result = []
    for e in items:
        booking = None
        customer = None
        if e.booking_id:
            booking = (await db.execute(select(Booking).where(Booking.id == e.booking_id))).scalar_one_or_none()
            if booking and booking.customer_id:
                customer = (await db.execute(select(Customer).where(Customer.id == booking.customer_id))).scalar_one_or_none()
        result.append(_esc_dict(e, booking, customer))
    return success_response(data={"items": result, "total": total})


@router.get("/history", summary="Escalation history [Admin/CCO]")
async def escalation_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation
    q = select(Escalation).order_by(Escalation.created_at.desc())
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [_esc_dict(e) for e in items], "total": total})


@router.get("/{escalation_id}", summary="Escalation details")
async def get_escalation(
    escalation_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation
    esc = (await db.execute(
        select(Escalation).where(Escalation.id == escalation_id, Escalation.is_active == True)
    )).scalar_one_or_none()
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return success_response(data=_esc_dict(esc))


@router.patch("/{escalation_id}", summary="Update escalation status/notes [Admin/CCO]")
async def patch_escalation(
    escalation_id: UUID,
    payload: PatchEscalationRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(
        select(Escalation).where(Escalation.id == escalation_id)
    )).scalar_one_or_none()
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")

    if payload.status:
        try:
            new_status = EscalationStatus(payload.status)
        except ValueError:
            raise HTTPException(400, f"Invalid status: {payload.status!r}")
        esc.status = new_status
        if new_status == EscalationStatus.RESOLVED and not esc.resolved_at:
            esc.resolved_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE
            esc.resolved_by = UUID(current_user["user_id"])
        if new_status == EscalationStatus.ESCALATED:
            esc.escalation_level = (getattr(esc, "escalation_level", 1) or 1) + 1

    if payload.resolution_notes is not None:
        esc.resolution_notes = payload.resolution_notes

    if payload.priority is not None:
        esc.priority = payload.priority

    if payload.assigned_to is not None:
        esc.assigned_to = UUID(payload.assigned_to) if payload.assigned_to else None

    await db.commit()
    await db.refresh(esc)
    return success_response(data=_esc_dict(esc), message="Escalation updated")


@router.post("/assign", summary="Assign escalation [Admin/CCO]")
async def assign_escalation(
    payload: EscalationActionRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    esc.assigned_to = UUID(payload.assigned_to) if payload.assigned_to else None
    esc.status = EscalationStatus.IN_PROGRESS
    await db.commit()
    return success_response(message="Escalation assigned")


@router.post("/resolve", summary="Resolve escalation [Admin/CCO]")
async def resolve_escalation(
    payload: EscalationActionRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    esc.status = EscalationStatus.RESOLVED
    esc.resolved_by = UUID(current_user["user_id"])
    esc.resolved_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE
    esc.resolution_notes = payload.notes
    await db.commit()
    return success_response(message="Escalation resolved")


@router.post("/escalate", summary="Escalate to higher authority [Admin/CCO]")
async def escalate(
    payload: EscalationActionRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.escalation import Escalation, EscalationStatus
    esc = (await db.execute(select(Escalation).where(Escalation.id == UUID(payload.escalation_id)))).scalar_one_or_none()
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    esc.status = EscalationStatus.ESCALATED
    esc.escalation_level = (getattr(esc, "escalation_level", 1) or 1) + 1
    esc.escalation_notes = payload.notes
    await db.commit()
    return success_response(message="Escalated to higher authority")
