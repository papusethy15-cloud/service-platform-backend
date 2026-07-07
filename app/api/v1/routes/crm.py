from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOrCCO, AnyStaff
from app.utils.response import success_response

router = APIRouter()

class AddNoteRequest(BaseModel):
    customer_id: str; note: str; note_type: str = "GENERAL"

class FollowupRequest(BaseModel):
    customer_id: str; due_date: str; subject: str; notes: Optional[str] = None

class TaskRequest(BaseModel):
    customer_id: Optional[str] = None; title: str; description: Optional[str] = None; due_date: Optional[str] = None; priority: str = "MEDIUM"

class CallLogRequest(BaseModel):
    customer_id: str
    booking_id: Optional[str] = None
    direction: str = "INBOUND"
    duration_seconds: Optional[int] = None
    outcome: str
    summary: str

@router.get("/customers", summary="CRM customer list [Staff]")
async def crm_customers(
    page: int = Query(1, ge=1), per_page: int = Query(20),
    search: str = Query(None), segment: str = Query(None),
    current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)
):
    from app.models.customer import Customer
    q = select(Customer).where(Customer.is_active == True)
    if search: q = q.where(Customer.name.ilike(f"%{search}%") | Customer.mobile.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{"id": str(c.id), "name": c.name, "mobile": c.mobile,
                   "email": c.email, "customer_code": c.customer_code,
                   "total_bookings": c.total_bookings} for c in items],
        "total": total, "page": page, "per_page": per_page
    })

@router.get("/customer/{customer_id}", summary="Full CRM profile [Staff]")
async def crm_profile(customer_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.customer import Customer
    from app.models.booking import Booking
    c = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
    if not c: raise HTTPException(status_code=404, detail="Customer not found")
    bookings = (await db.execute(select(Booking).where(Booking.customer_id == customer_id).order_by(Booking.created_at.desc()).limit(5))).scalars().all()
    return success_response(data={
        "id": str(c.id), "name": c.name, "mobile": c.mobile,
        "email": c.email, "customer_code": c.customer_code,
        "recent_bookings": [{"id": str(b.id), "booking_number": b.booking_number,
                              "status": b.status.value, "total_amount": b.total_amount} for b in bookings]
    })

@router.post("/notes", summary="Add CRM note [Staff]")
async def add_note(payload: AddNoteRequest, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMNote
    note = CRMNote(customer_id=UUID(payload.customer_id), added_by=UUID(current_user["user_id"]),
                   note=payload.note, note_type=payload.note_type)
    db.add(note); await db.commit()
    return success_response(data={"id": str(note.id)}, message="Note added")

@router.get("/notes", summary="List CRM notes for a customer [Staff]")
async def list_notes(customer_id: str = Query(...), current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMNote
    from app.models.user import User
    rows = (await db.execute(
        select(CRMNote, User.name).join(User, User.id == CRMNote.added_by)
        .where(CRMNote.customer_id == UUID(customer_id), CRMNote.is_active == True)
        .order_by(CRMNote.created_at.desc())
    )).all()
    return success_response(data=[{
        "id": str(n.id), "customer_id": str(n.customer_id), "added_by": str(n.added_by),
        "added_by_name": added_by_name, "note": n.note, "note_type": n.note_type,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n, added_by_name in rows])

@router.post("/followup", summary="Create follow-up [Staff]")
async def create_followup(payload: FollowupRequest, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMFollowup
    fu = CRMFollowup(customer_id=UUID(payload.customer_id), created_by=UUID(current_user["user_id"]),
                     subject=payload.subject, notes=payload.notes,
                     due_date=datetime.fromisoformat(payload.due_date))
    db.add(fu); await db.commit()
    return success_response(data={"id": str(fu.id)}, message="Follow-up created")

@router.get("/followups", summary="Follow-up list [Staff]")
async def list_followups(page: int = Query(1, ge=1), per_page: int = Query(20),
                         current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMFollowup
    items = (await db.execute(select(CRMFollowup).where(CRMFollowup.is_active == True)
                              .order_by(CRMFollowup.due_date).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data=[{"id": str(f.id), "customer_id": str(f.customer_id),
                                    "subject": f.subject, "due_date": f.due_date.isoformat(),
                                    "status": f.status} for f in items])

@router.patch("/followups/{followup_id}/done", summary="Mark follow-up done [Staff]")
async def mark_followup_done(followup_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMFollowup
    fu = (await db.execute(select(CRMFollowup).where(CRMFollowup.id == followup_id))).scalar_one_or_none()
    if not fu:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Follow-up not found")
    fu.status = "DONE"
    fu.is_active = False
    await db.commit()
    return success_response(message="Follow-up marked done")

@router.post("/task", summary="Create CRM task [Staff]")
async def create_task(payload: TaskRequest, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMTask
    task = CRMTask(created_by=UUID(current_user["user_id"]),
                   customer_id=UUID(payload.customer_id) if payload.customer_id else None,
                   title=payload.title, description=payload.description, priority=payload.priority)
    db.add(task); await db.commit()
    return success_response(data={"id": str(task.id)}, message="Task created")

@router.get("/tasks", summary="Task list [Staff]")
async def list_tasks(page: int = Query(1, ge=1), per_page: int = Query(20),
                     current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CRMTask
    items = (await db.execute(select(CRMTask).where(CRMTask.is_active == True)
                              .order_by(CRMTask.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data=[{"id": str(t.id), "title": t.title, "priority": t.priority,
                                    "status": t.status, "due_date": t.due_date.isoformat() if t.due_date else None} for t in items])

@router.post("/call-logs", summary="Log a customer call [Staff]")
async def create_call_log(payload: CallLogRequest, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.crm import CallLog
    log = CallLog(
        customer_id=UUID(payload.customer_id),
        cco_id=UUID(current_user["user_id"]),
        booking_id=UUID(payload.booking_id) if payload.booking_id else None,
        direction=payload.direction,
        duration_seconds=payload.duration_seconds,
        outcome=payload.outcome,
        summary=payload.summary,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return success_response(data={"id": str(log.id)}, message="Call logged")

@router.get("/call-logs", summary="List call logs — global or by customer [Staff]")
async def list_call_logs(
    customer_id: str = Query(None),
    outcome: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.crm import CallLog
    from app.models.user import User
    q = (
        select(CallLog, User.name)
        .join(User, User.id == CallLog.cco_id)
        .where(CallLog.is_active == True)
        .order_by(CallLog.created_at.desc())
    )
    if customer_id:
        q = q.where(CallLog.customer_id == UUID(customer_id))
    if outcome:
        q = q.where(CallLog.outcome == outcome)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).all()
    return success_response(data={
        "items": [{
            "id": str(log.id), "customer_id": str(log.customer_id), "cco_id": str(log.cco_id),
            "cco_name": cco_name, "booking_id": str(log.booking_id) if log.booking_id else None,
            "direction": log.direction, "duration_seconds": log.duration_seconds,
            "outcome": log.outcome, "summary": log.summary,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        } for log, cco_name in rows],
        "total": total, "page": page, "per_page": per_page,
    })

@router.get("/segments", summary="Customer segments [Staff]")
async def customer_segments(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    return success_response(data=[
        {"key": "new", "label": "New Customers", "description": "First booking in last 30 days"},
        {"key": "active", "label": "Active Customers", "description": "Booking in last 90 days"},
        {"key": "at_risk", "label": "At Risk", "description": "No booking in 90-180 days"},
        {"key": "churned", "label": "Churned", "description": "No booking in 180+ days"},
        {"key": "vip", "label": "VIP", "description": "5+ bookings or ₹10,000+ spent"},
        {"key": "amc", "label": "AMC Holders", "description": "Active AMC subscription"},
    ])
