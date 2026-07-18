"""CCO Attendance and Salary routes.

CCO check-in/check-out lifecycle
─────────────────────────────────
• POST /cco/attendance/check-in      — called by CCO portal on login (auto)
• POST /cco/attendance/check-out     — called on logout or idle-timeout (auto)
• GET  /cco/attendance/today         — CCO: own today record
• GET  /admin/cco-attendance         — Admin: list CCO attendance (filterable)
• GET  /admin/cco-attendance/{uid}/month — Admin: monthly attendance for one CCO
• GET  /admin/cco-salary             — Admin: all CCOs with salary info
• POST /admin/cco-salary/{uid}/generate — Admin: generate/preview salary slip
• POST /admin/cco-salary/{uid}/pay   — Admin: mark salary as paid
• GET  /cco/salary/slips             — CCO: own salary slips list
• GET  /cco/salary/slips/{slip_id}   — CCO: one salary slip detail
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime, timezone, timedelta

from app.core.database import get_db
from app.api.deps import AdminOnly, AnyAuthenticated, AdminOrCCO
from app.utils.response import success_response, iso

router = APIRouter()

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_today() -> date:
    return datetime.now(timezone.utc).astimezone(_IST).date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_att(att, user_name: str | None = None) -> dict:
    """Format a CcoAttendance row."""
    total_sec = att.accumulated_seconds or 0
    is_active = bool(att.check_in and not att.check_out)
    if is_active and att.check_in:
        ci = att.check_in
        if ci.tzinfo is None:
            ci = ci.replace(tzinfo=timezone.utc)
        total_sec += (_now_utc() - ci).total_seconds()
    return {
        "id":               str(att.id),
        "user_id":          str(att.user_id),
        "user_name":        user_name,
        "date":             str(att.date),
        "check_in":         iso(att.check_in)  if att.check_in  else None,
        "check_out":        iso(att.check_out) if att.check_out else None,
        "is_active":        is_active,
        "hours_worked":     round(total_sec / 3600, 2),
        "status":           att.status,
        "notes":            att.notes,
    }


# ── CCO: Auto check-in on login ───────────────────────────────────────────────
@router.post("/check-in", summary="CCO: auto check-in on portal login")
async def cco_check_in(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoAttendance
    from app.models.user import User, UserRole

    uid = UUID(current_user["user_id"])
    # Verify this is actually a CCO (or admin calling on behalf)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user or user.role not in (UserRole.CCO, UserRole.ADMIN, UserRole.SUPER_ADMIN):
        raise HTTPException(403, "Only CCO users can call this endpoint")

    today = _ist_today()
    now   = _now_utc()

    # Upsert: one row per (user_id, date)
    att = (await db.execute(
        select(CcoAttendance).where(
            CcoAttendance.user_id == uid,
            CcoAttendance.date    == today,
        )
    )).scalar_one_or_none()

    if att is None:
        # First login of the day
        att = CcoAttendance(
            user_id=uid,
            date=today,
            check_in=now,
            check_out=None,
            accumulated_seconds=0,
            status="PRESENT",
        )
        db.add(att)
    else:
        # Same day re-login: update check_in timestamp; check_out cleared (active again)
        # Previous session time already in accumulated_seconds
        att.check_in  = now
        att.check_out = None

    await db.commit()
    await db.refresh(att)
    return success_response(data=_fmt_att(att, user.name), message="Checked in")


# ── CCO: Auto check-out on logout / idle ─────────────────────────────────────
@router.post("/check-out", summary="CCO: auto check-out on portal logout or idle timeout")
async def cco_check_out(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoAttendance
    from app.models.user import User, UserRole

    uid = UUID(current_user["user_id"])
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    today = _ist_today()
    now   = _now_utc()

    att = (await db.execute(
        select(CcoAttendance).where(
            CcoAttendance.user_id == uid,
            CcoAttendance.date    == today,
        )
    )).scalar_one_or_none()

    if att is None:
        raise HTTPException(404, "No attendance record found for today")

    if att.check_in and not att.check_out:
        # Close the current session — add elapsed time
        ci = att.check_in
        if ci.tzinfo is None:
            ci = ci.replace(tzinfo=timezone.utc)
        session_secs = max(0, int((_now_utc() - ci).total_seconds()))
        att.accumulated_seconds = (att.accumulated_seconds or 0) + session_secs
        att.check_out = now

    await db.commit()
    await db.refresh(att)
    return success_response(data=_fmt_att(att, user.name), message="Checked out")


# ── CCO: Today's attendance ──────────────────────────────────────────────────
@router.get("/today", summary="CCO: own today's attendance record")
async def cco_today_attendance(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoAttendance
    from app.models.user import User

    uid = UUID(current_user["user_id"])
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    today = _ist_today()
    att = (await db.execute(
        select(CcoAttendance).where(
            CcoAttendance.user_id == uid,
            CcoAttendance.date    == today,
        )
    )).scalar_one_or_none()
    if not att:
        return success_response(data=None, message="No record today")
    return success_response(data=_fmt_att(att, user.name if user else None))


# ── Admin: list CCO attendance ────────────────────────────────────────────────
@router.get("/admin/list", summary="Admin: list CCO attendance records")
async def admin_list_cco_attendance(
    user_id:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    page:      int = Query(1, ge=1),
    per_page:  int = Query(30, ge=1, le=100),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoAttendance
    from app.models.user import User, UserRole

    q = (
        select(CcoAttendance, User.name)
        .join(User, User.id == CcoAttendance.user_id)
        .where(User.role == UserRole.CCO)
    )
    if user_id:
        q = q.where(CcoAttendance.user_id == UUID(user_id))
    if date_from:
        q = q.where(CcoAttendance.date >= date.fromisoformat(date_from))
    if date_to:
        q = q.where(CcoAttendance.date <= date.fromisoformat(date_to))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows  = (await db.execute(q.order_by(CcoAttendance.date.desc()).offset((page - 1) * per_page).limit(per_page))).all()
    items = [_fmt_att(att, name) for att, name in rows]
    return success_response(data={"items": items, "total": total, "page": page, "per_page": per_page})


# ── Admin: monthly attendance for one CCO ─────────────────────────────────────
@router.get("/admin/month/{user_id}", summary="Admin: monthly CCO attendance")
async def admin_cco_month_attendance(
    user_id: str,
    month: int = Query(..., ge=1, le=12),
    year:  int = Query(..., ge=2020),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoAttendance
    from app.models.user import User
    import calendar

    uid  = UUID(user_id)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    rows = (await db.execute(
        select(CcoAttendance).where(
            CcoAttendance.user_id == uid,
            func.extract('month', CcoAttendance.date) == month,
            func.extract('year',  CcoAttendance.date) == year,
        ).order_by(CcoAttendance.date)
    )).scalars().all()

    days_in_month = calendar.monthrange(year, month)[1]
    total_hours   = sum(
        round(((r.accumulated_seconds or 0) +
               (max(0, (_now_utc() - (r.check_in.replace(tzinfo=timezone.utc) if r.check_in and r.check_in.tzinfo is None else r.check_in)).total_seconds())
                if r.check_in and not r.check_out else 0)
               ) / 3600, 2)
        for r in rows
    )

    return success_response(data={
        "user_id":       str(uid),
        "user_name":     user.name,
        "month":         month,
        "year":          year,
        "days_in_month": days_in_month,
        "present_days":  len([r for r in rows if r.status == "PRESENT"]),
        "total_hours":   round(total_hours, 2),
        "records":       [_fmt_att(r, user.name) for r in rows],
        # Salary structure stored on user
        "monthly_salary":   user.monthly_salary or 0,
        "petrol_amount":    user.petrol_amount or 0,
        "mobile_recharge":  user.mobile_recharge or 0,
        "bonus_amount":     user.bonus_amount or 0,
        "hra_amount":       user.hra_amount or 0,
        "other_allowances": user.other_allowances or 0,
        "salary_notes":     user.salary_notes,
        "payout_upi_id":          user.payout_upi_id,
        "payout_bank_account":    user.payout_bank_account,
        "payout_bank_ifsc":       user.payout_bank_ifsc,
        "payout_bank_name":       user.payout_bank_name,
        "payout_account_holder":  user.payout_account_holder,
    })


# ── Admin: list all CCOs with salary info ─────────────────────────────────────
@router.get("/admin/cco-list", summary="Admin: all CCO users with salary structure")
async def admin_cco_list(
    search:   Optional[str] = Query(None),
    page:     int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import User, UserRole

    q = select(User).where(User.role == UserRole.CCO)
    if search:
        q = q.where(User.name.ilike(f"%{search}%") | User.mobile.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    users = (await db.execute(q.order_by(User.name).offset((page-1)*per_page).limit(per_page))).scalars().all()

    def _fmt_user(u):
        return {
            "id": str(u.id), "name": u.name, "email": u.email, "mobile": u.mobile,
            "monthly_salary": u.monthly_salary or 0,
            "petrol_amount":  u.petrol_amount  or 0,
            "mobile_recharge": u.mobile_recharge or 0,
            "bonus_amount":   u.bonus_amount   or 0,
            "hra_amount":     u.hra_amount      or 0,
            "other_allowances": u.other_allowances or 0,
            "salary_notes":   u.salary_notes,
            "payout_upi_id":         u.payout_upi_id,
            "payout_bank_account":   u.payout_bank_account,
            "payout_bank_ifsc":      u.payout_bank_ifsc,
            "payout_bank_name":      u.payout_bank_name,
            "payout_account_holder": u.payout_account_holder,
        }

    return success_response(data={"items": [_fmt_user(u) for u in users], "total": total})


# ── Admin: update CCO salary structure / bank details ─────────────────────────
class CcoSalaryUpdate(BaseModel):
    monthly_salary:   Optional[float] = None
    petrol_amount:    Optional[float] = None
    mobile_recharge:  Optional[float] = None
    bonus_amount:     Optional[float] = None
    hra_amount:       Optional[float] = None
    other_allowances: Optional[float] = None
    salary_notes:     Optional[str]   = None
    payout_upi_id:         Optional[str] = None
    payout_bank_account:   Optional[str] = None
    payout_bank_ifsc:      Optional[str] = None
    payout_bank_name:      Optional[str] = None
    payout_account_holder: Optional[str] = None

@router.put("/admin/cco/{user_id}/salary-structure", summary="Admin: update CCO salary structure")
async def admin_update_cco_salary_structure(
    user_id: str,
    body: CcoSalaryUpdate,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import User, UserRole
    uid  = UUID(user_id)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user or user.role != UserRole.CCO:
        raise HTTPException(404, "CCO user not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(user, field, val)
    await db.commit()
    return success_response(message="Salary structure updated")


# ── Admin: generate CCO salary slip ──────────────────────────────────────────
class GenerateSalaryBody(BaseModel):
    month: int
    year:  int
    deductions:     Optional[float] = 0
    deduction_notes: Optional[str]  = None
    bonus_amount:   Optional[float] = None   # override if needed
    salary_notes:   Optional[str]   = None

@router.post("/admin/cco/{user_id}/generate-salary", summary="Admin: generate CCO salary slip")
async def admin_generate_cco_salary(
    user_id: str,
    body: GenerateSalaryBody,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import User, UserRole
    from app.models.cco_attendance import CcoAttendance, CcoSalarySettlement
    import calendar

    uid  = UUID(user_id)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user or user.role != UserRole.CCO:
        raise HTTPException(404, "CCO user not found")

    # Attendance summary for the month
    rows = (await db.execute(
        select(CcoAttendance).where(
            CcoAttendance.user_id == uid,
            func.extract('month', CcoAttendance.date) == body.month,
            func.extract('year',  CcoAttendance.date) == body.year,
        )
    )).scalars().all()

    days_in_month = calendar.monthrange(body.year, body.month)[1]
    present_days  = len([r for r in rows if r.status == "PRESENT"])
    total_secs    = sum(r.accumulated_seconds or 0 for r in rows)
    total_hours   = round(total_secs / 3600, 2)

    # Salary components
    ms   = float(user.monthly_salary   or 0)
    pet  = float(user.petrol_amount    or 0)
    mob  = float(user.mobile_recharge  or 0)
    bon  = float(body.bonus_amount if body.bonus_amount is not None else (user.bonus_amount or 0))
    hra  = float(user.hra_amount       or 0)
    oth  = float(user.other_allowances or 0)
    ded  = float(body.deductions       or 0)
    gross = ms + pet + mob + bon + hra + oth
    net   = max(0.0, gross - ded)

    # Upsert slip
    existing = (await db.execute(
        select(CcoSalarySettlement).where(
            CcoSalarySettlement.user_id == uid,
            CcoSalarySettlement.month   == body.month,
            CcoSalarySettlement.year    == body.year,
        )
    )).scalar_one_or_none()

    if existing:
        slip = existing
    else:
        slip = CcoSalarySettlement(user_id=uid, month=body.month, year=body.year)
        db.add(slip)

    slip.monthly_salary   = ms
    slip.petrol_amount    = pet
    slip.mobile_recharge  = mob
    slip.bonus_amount     = bon
    slip.hra_amount       = hra
    slip.other_allowances = oth
    slip.deductions       = ded
    slip.deduction_notes  = body.deduction_notes
    slip.total_days       = days_in_month
    slip.present_days     = present_days
    slip.total_hours      = total_hours
    slip.gross_salary     = gross
    slip.net_salary       = net
    slip.salary_notes     = body.salary_notes
    if not existing:
        slip.status = "PENDING"

    await db.commit()
    await db.refresh(slip)

    return success_response(data={
        "id":              str(slip.id),
        "user_id":         str(uid),
        "user_name":       user.name,
        "month":           slip.month,
        "year":            slip.year,
        "monthly_salary":  slip.monthly_salary,
        "petrol_amount":   slip.petrol_amount,
        "mobile_recharge": slip.mobile_recharge,
        "bonus_amount":    slip.bonus_amount,
        "hra_amount":      slip.hra_amount,
        "other_allowances": slip.other_allowances,
        "deductions":      slip.deductions,
        "deduction_notes": slip.deduction_notes,
        "total_days":      slip.total_days,
        "present_days":    slip.present_days,
        "total_hours":     slip.total_hours,
        "gross_salary":    slip.gross_salary,
        "net_salary":      slip.net_salary,
        "status":          slip.status,
        "salary_notes":    slip.salary_notes,
    }, message="Salary slip generated")


# ── Admin: mark CCO salary as paid ───────────────────────────────────────────
class PaySalaryBody(BaseModel):
    slip_id:        str
    payment_method: str   # UPI | BANK | CASH
    payment_ref:    Optional[str] = None

@router.post("/admin/cco/pay-salary", summary="Admin: mark CCO salary slip as paid")
async def admin_pay_cco_salary(
    body: PaySalaryBody,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoSalarySettlement
    from app.models.user import User

    slip = (await db.execute(
        select(CcoSalarySettlement).where(CcoSalarySettlement.id == UUID(body.slip_id))
    )).scalar_one_or_none()
    if not slip:
        raise HTTPException(404, "Salary slip not found")
    if slip.status == "PAID":
        raise HTTPException(400, "Already paid")

    slip.status         = "PAID"
    slip.payment_method = body.payment_method
    slip.payment_ref    = body.payment_ref
    slip.paid_at        = _now_utc()
    slip.paid_by        = UUID(current_user["user_id"])
    await db.commit()
    return success_response(message="Salary marked as paid")


# ── CCO: own salary slips ─────────────────────────────────────────────────────
@router.get("/my-slips", summary="CCO: list own salary slips")
async def cco_my_salary_slips(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoSalarySettlement
    uid = UUID(current_user["user_id"])
    rows = (await db.execute(
        select(CcoSalarySettlement).where(CcoSalarySettlement.user_id == uid)
        .order_by(CcoSalarySettlement.year.desc(), CcoSalarySettlement.month.desc())
    )).scalars().all()

    def _fmt(s):
        return {
            "id": str(s.id), "month": s.month, "year": s.year,
            "gross_salary": s.gross_salary, "net_salary": s.net_salary,
            "present_days": s.present_days, "total_hours": s.total_hours,
            "status": s.status, "paid_at": iso(s.paid_at) if s.paid_at else None,
        }

    return success_response(data={"slips": [_fmt(s) for s in rows]})


# ── CCO: one salary slip detail ───────────────────────────────────────────────
@router.get("/my-slips/{slip_id}", summary="CCO: salary slip detail")
async def cco_salary_slip_detail(
    slip_id: str,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.cco_attendance import CcoSalarySettlement
    from app.models.user import User

    uid  = UUID(current_user["user_id"])
    slip = (await db.execute(
        select(CcoSalarySettlement).where(
            CcoSalarySettlement.id == UUID(slip_id),
            CcoSalarySettlement.user_id == uid,
        )
    )).scalar_one_or_none()
    if not slip:
        raise HTTPException(404, "Slip not found")

    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    return success_response(data={
        "id": str(slip.id),
        "month": slip.month, "year": slip.year,
        "month_label": f"{MONTHS[slip.month-1]} {slip.year}",
        "user_name": user.name if user else "",
        "user_mobile": user.mobile if user else "",
        "monthly_salary": slip.monthly_salary,
        "petrol_amount":  slip.petrol_amount,
        "mobile_recharge": slip.mobile_recharge,
        "bonus_amount":   slip.bonus_amount,
        "hra_amount":     slip.hra_amount,
        "other_allowances": slip.other_allowances,
        "deductions":     slip.deductions,
        "deduction_notes": slip.deduction_notes,
        "total_days":     slip.total_days,
        "present_days":   slip.present_days,
        "total_hours":    slip.total_hours,
        "gross_salary":   slip.gross_salary,
        "net_salary":     slip.net_salary,
        "status":         slip.status,
        "payment_method": slip.payment_method,
        "payment_ref":    slip.payment_ref,
        "paid_at":        iso(slip.paid_at) if slip.paid_at else None,
        "salary_notes":   slip.salary_notes,
    })
