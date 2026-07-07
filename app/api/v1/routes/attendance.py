from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime, timezone
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()


class CheckInRequest(BaseModel):
    latitude:  Optional[float] = None
    longitude: Optional[float] = None


class CheckOutRequest(BaseModel):
    notes: Optional[str] = None


def _fmt_att(att) -> dict:
    """Format one Attendance row for API response.

    hours_worked reflects TOTAL time worked today across all check-in/check-out
    sessions (accumulated_seconds), plus the live elapsed time of the current
    session if the technician is still checked in (check_in set, check_out null).
    """
    check_in_dt  = att.check_in.isoformat()  if att.check_in  else None
    check_out_dt = att.check_out.isoformat() if att.check_out else None
    total_seconds = att.accumulated_seconds or 0
    is_active = bool(att.check_in and not att.check_out)
    if is_active:
        check_in_aware = att.check_in
        if check_in_aware.tzinfo is None:
            # asyncpg sometimes hands back a naive datetime even though the
            # column is TIMESTAMPTZ; treat it as UTC to match datetime.now(utc).
            check_in_aware = check_in_aware.replace(tzinfo=timezone.utc)
        total_seconds += (datetime.now(timezone.utc) - check_in_aware).total_seconds()
    hours_worked = round(total_seconds / 3600, 2) if total_seconds else 0.0
    return {
        "id":            str(att.id),
        "date":          str(att.date),
        "status":        att.status,
        "check_in":      check_in_dt,
        "check_out":     check_out_dt,
        "is_active":     is_active,
        "hours_worked":  hours_worked,
        "notes":         att.notes,
    }


# ── Captain: get today's attendance record ─────────────────────────────────
@router.get("/today", summary="Today's attendance for logged-in technician")
async def get_today_attendance(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    tech = (await db.execute(
        select(Technician).where(Technician.user_id == UUID(current_user["user_id"]))
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")
    today = date.today()
    att = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == tech.id,
            Attendance.date == today,
        )
    )).scalar_one_or_none()
    if not att:
        raise HTTPException(404, "No attendance record for today")
    return success_response(data=_fmt_att(att))


# ── Captain: get own attendance history ────────────────────────────────────
@router.get("/me", summary="My attendance history [Technician]")
async def my_attendance(
    page:     int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    tech = (await db.execute(
        select(Technician).where(Technician.user_id == UUID(current_user["user_id"]))
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    q = select(Attendance).where(Attendance.technician_id == tech.id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows  = (await db.execute(
        q.order_by(Attendance.date.desc())
         .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    # Summary
    all_rows = (await db.execute(
        select(Attendance).where(Attendance.technician_id == tech.id)
    )).scalars().all()
    days_present = sum(1 for r in all_rows if r.status == "PRESENT")
    total_hours  = sum((r.accumulated_seconds or 0) for r in all_rows) / 3600

    return success_response(data={
        "summary": {
            "days_present":  days_present,
            "total_hours":   round(total_hours, 2),
        },
        "items":    [_fmt_att(r) for r in rows],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
    })


# ── Check-in: allows multiple sessions per day ─────────────────────────────
@router.post("/check-in", summary="Technician check-in (online)")
async def check_in(
    payload: CheckInRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    tech = (await db.execute(
        select(Technician).where(Technician.user_id == UUID(current_user["user_id"]))
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    today = date.today()
    existing = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == tech.id,
            Attendance.date == today,
        )
    )).scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing:
        if existing.check_in and not existing.check_out:
            # Already checked in this session, not yet checked out
            raise HTTPException(400, "Already checked in. Please check out first.")
        # Re-check-in (new session same day): start a new session.
        # accumulated_seconds already holds the total from prior sessions today,
        # so it is left untouched here.
        existing.check_in     = now
        existing.check_out    = None
        existing.check_in_lat = payload.latitude
        existing.check_in_lng = payload.longitude
        existing.status       = "PRESENT"
        att = existing
    else:
        att = Attendance(
            technician_id=tech.id,
            date=today,
            check_in=now,
            check_in_lat=payload.latitude,
            check_in_lng=payload.longitude,
            accumulated_seconds=0,
            status="PRESENT",
        )
        db.add(att)

    # Also set technician online
    tech.is_online = True
    await db.commit()
    return success_response(data=_fmt_att(att), message="Checked in — you are now online")


# ── Check-out ──────────────────────────────────────────────────────────────
@router.post("/check-out", summary="Technician check-out (offline)")
async def check_out(
    payload: CheckOutRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.attendance import Attendance
    from app.models.technician import Technician
    tech = (await db.execute(
        select(Technician).where(Technician.user_id == UUID(current_user["user_id"]))
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    att = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == tech.id,
            Attendance.date == date.today(),
        )
    )).scalar_one_or_none()

    if not att or not att.check_in:
        raise HTTPException(400, "Not checked in yet")
    if att.check_out:
        raise HTTPException(400, "Already checked out. Check in again to start a new session.")

    now = datetime.now(timezone.utc)
    check_in_aware = att.check_in
    if check_in_aware.tzinfo is None:
        check_in_aware = check_in_aware.replace(tzinfo=timezone.utc)
    elapsed = (now - check_in_aware).total_seconds()
    att.accumulated_seconds = (att.accumulated_seconds or 0) + max(0, int(elapsed))
    att.check_out = now
    att.notes     = payload.notes
    # Also set technician offline
    tech.is_online = False
    await db.commit()
    return success_response(data=_fmt_att(att), message="Checked out — you are now offline")


# ── Admin: list attendance ─────────────────────────────────────────────────
@router.get("", summary="Attendance list [Admin/CCO]")
async def list_attendance(
    page:          int            = Query(1, ge=1),
    per_page:      int            = Query(20),
    technician_id: Optional[str] = None,
    date_from:     Optional[date] = None,
    date_to:       Optional[date] = None,
    current_user:  dict           = Depends(AdminOrCCO),
    db:            AsyncSession   = Depends(get_db),
):
    from app.models.attendance import Attendance
    q = select(Attendance)
    if technician_id: q = q.where(Attendance.technician_id == UUID(technician_id))
    if date_from:     q = q.where(Attendance.date >= date_from)
    if date_to:       q = q.where(Attendance.date <= date_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(
        q.order_by(Attendance.date.desc())
         .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return success_response(data={
        "items": [_fmt_att(a) for a in items],
        "total": total,
    })
