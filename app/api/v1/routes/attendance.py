from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CheckInRequest(BaseModel):
    latitude: Optional[float] = None; longitude: Optional[float] = None

class CheckOutRequest(BaseModel):
    notes: Optional[str] = None

@router.post("/check-in", summary="Technician check-in")
async def check_in(payload: CheckInRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    from datetime import timezone
    tech = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    if not tech: raise HTTPException(404, "Technician not found")
    today = date.today()
    existing = (await db.execute(select(Attendance).where(Attendance.technician_id == tech.id, Attendance.date == today))).scalar_one_or_none()
    if existing and existing.check_in: raise HTTPException(400, "Already checked in today")
    att = existing or Attendance(technician_id=tech.id, date=today)
    att.check_in = datetime.now(timezone.utc)
    att.check_in_lat = payload.latitude; att.check_in_lng = payload.longitude; att.status = "PRESENT"
    db.add(att); await db.commit()
    return success_response(data={"check_in": att.check_in.isoformat()}, message="Checked in")

@router.post("/check-out", summary="Technician check-out")
async def check_out(payload: CheckOutRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    from datetime import timezone
    tech = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
    if not tech: raise HTTPException(404, "Technician not found")
    att = (await db.execute(select(Attendance).where(Attendance.technician_id == tech.id, Attendance.date == date.today()))).scalar_one_or_none()
    if not att or not att.check_in: raise HTTPException(400, "Not checked in yet")
    att.check_out = datetime.now(timezone.utc); att.notes = payload.notes
    await db.commit()
    return success_response(data={"check_out": att.check_out.isoformat()}, message="Checked out")

@router.get("", summary="Attendance list [Admin/CCO]")
async def list_attendance(page: int = Query(1, ge=1), per_page: int = Query(20),
                          technician_id: Optional[str] = None, date_from: Optional[date] = None,
                          date_to: Optional[date] = None,
                          current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.attendance import Attendance
    q = select(Attendance)
    if technician_id: q = q.where(Attendance.technician_id == UUID(technician_id))
    if date_from: q = q.where(Attendance.date >= date_from)
    if date_to: q = q.where(Attendance.date <= date_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(Attendance.date.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(a.id), "technician_id": str(a.technician_id),
                                              "date": str(a.date), "status": a.status,
                                              "check_in": a.check_in.isoformat() if a.check_in else None,
                                              "check_out": a.check_out.isoformat() if a.check_out else None} for a in items],
                                   "total": total})
