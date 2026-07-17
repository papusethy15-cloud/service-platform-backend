"""Salary settlement routes for salary-group technicians."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from pydantic import BaseModel
from typing import Optional
from uuid import UUID
import calendar
from datetime import datetime, timezone

from app.core.database import get_db
from app.api.deps import AdminOnly
from app.utils.response import success_response, iso

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class GenerateSalaryRequest(BaseModel):
    technician_id: str
    month: int
    year: int
    # Admin can override these; if null, taken from group
    monthly_salary:   Optional[float] = None
    petrol_amount:    Optional[float] = None
    mobile_recharge:  Optional[float] = None
    bonus_amount:     Optional[float] = None
    hra_amount:       Optional[float] = None
    other_allowances: Optional[float] = None
    deductions:       Optional[float] = 0.0
    deduction_notes:  Optional[str]   = None
    admin_notes:      Optional[str]   = None


class UpdateSalaryRequest(BaseModel):
    monthly_salary:   Optional[float] = None
    petrol_amount:    Optional[float] = None
    mobile_recharge:  Optional[float] = None
    bonus_amount:     Optional[float] = None
    hra_amount:       Optional[float] = None
    other_allowances: Optional[float] = None
    deductions:       Optional[float] = None
    deduction_notes:  Optional[str]   = None
    admin_notes:      Optional[str]   = None


class PaySalaryRequest(BaseModel):
    payout_method: str   # UPI | BANK
    payment_reference: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_salary_tech(technician_id: UUID, db: AsyncSession):
    """Return (Technician, CommissionGroup) or raise 404/400."""
    from app.models.technician import Technician
    from app.models.commission import CommissionGroup, CommissionGroupAssignment

    tech = (await db.execute(
        select(Technician).where(Technician.id == technician_id)
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    asgn = (await db.execute(
        select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == technician_id)
    )).scalar_one_or_none()

    group = None
    if asgn:
        group = (await db.execute(
            select(CommissionGroup).where(CommissionGroup.id == asgn.group_id)
        )).scalar_one_or_none()

    if not group or not group.is_salary_group:
        raise HTTPException(400, "Technician is not in a salary group")

    return tech, group


def _fmt_settlement(s, tech_name: str = None, tech_mobile: str = None, group_name: str = None) -> dict:
    return {
        "id":                   str(s.id),
        "technician_id":        str(s.technician_id),
        "technician_name":      tech_name,
        "technician_mobile":    tech_mobile,
        "commission_group_id":  str(s.commission_group_id) if s.commission_group_id else None,
        "commission_group_name": group_name,
        "month":                s.month,
        "year":                 s.year,
        "monthly_salary":       s.monthly_salary or 0,
        "petrol_amount":        s.petrol_amount or 0,
        "mobile_recharge":      s.mobile_recharge or 0,
        "bonus_amount":         s.bonus_amount or 0,
        "hra_amount":           s.hra_amount or 0,
        "other_allowances":     s.other_allowances or 0,
        "deductions":           s.deductions or 0,
        "deduction_notes":      s.deduction_notes,
        "market_reimbursement": s.market_reimbursement or 0,
        "gross_salary":         s.gross_salary or 0,
        "net_salary":           s.net_salary or 0,
        "total_bookings":       s.total_bookings or 0,
        "total_hours_worked":   s.total_hours_worked or 0,
        "attendance_days":      s.attendance_days or 0,
        "status":               s.status,
        "admin_notes":          s.admin_notes,
        "paid_at":              iso(s.paid_at) if s.paid_at else None,
        "wallet_txn_id":        str(s.wallet_txn_id) if s.wallet_txn_id else None,
        "payment_reference":    s.payment_reference,
        "payout_method":        s.payout_method,
        "created_at":           iso(s.created_at),
        "updated_at":           iso(s.updated_at),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/groups/salary-technicians", summary="List all salary group technicians with monthly stats [Admin]")
async def list_salary_technicians(
    month: int = Query(..., ge=1, le=12),
    year:  int = Query(...),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """Returns all technicians in salary groups with booking count, attendance, and settlement status for the given month/year."""
    from app.models.technician import Technician
    from app.models.commission import CommissionGroup, CommissionGroupAssignment
    from app.models.booking import Booking, BookingStatus
    from app.models.attendance import Attendance
    from app.models.commission import SalarySettlement
    from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
    from datetime import date

    sd = date(year, month, 1)
    ed = date(year, month, calendar.monthrange(year, month)[1])
    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    # All salary groups
    salary_groups = (await db.execute(
        select(CommissionGroup).where(CommissionGroup.is_salary_group == True, CommissionGroup.is_active == True)
    )).scalars().all()

    if not salary_groups:
        return success_response(data={"technicians": [], "total": 0})

    group_ids = [g.id for g in salary_groups]
    group_map = {g.id: g for g in salary_groups}

    # Assignments for salary groups
    assignments = (await db.execute(
        select(CommissionGroupAssignment).where(CommissionGroupAssignment.group_id.in_(group_ids))
    )).scalars().all()

    if not assignments:
        return success_response(data={"technicians": [], "total": 0})

    tech_ids = [a.technician_id for a in assignments]
    asgn_map = {a.technician_id: a for a in assignments}

    technicians = (await db.execute(
        select(Technician).where(Technician.id.in_(tech_ids))
    )).scalars().all()

    tech_map = {t.id: t for t in technicians}

    # Booking counts
    booking_counts: dict = {}
    bk_rows = (await db.execute(
        select(Booking.technician_id, func.count(Booking.id).label("cnt"))
        .where(
            Booking.technician_id.in_(tech_ids),
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        )
        .group_by(Booking.technician_id)
    )).all()
    for row in bk_rows:
        booking_counts[row.technician_id] = row.cnt

    # Attendance
    att_rows = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id.in_(tech_ids),
            Attendance.date >= sd,
            Attendance.date <= ed,
        )
    )).scalars().all()

    att_by_tech: dict = {}
    for att in att_rows:
        att_by_tech.setdefault(att.technician_id, []).append(att)

    # Existing settlements
    settlements = (await db.execute(
        select(SalarySettlement).where(
            SalarySettlement.technician_id.in_(tech_ids),
            SalarySettlement.month == month,
            SalarySettlement.year == year,
        )
    )).scalars().all()
    settle_map = {s.technician_id: s for s in settlements}

    results = []
    for tech in technicians:
        asgn = asgn_map.get(tech.id)
        group = group_map.get(asgn.group_id) if asgn else None
        att_list = att_by_tech.get(tech.id, [])
        att_days = sum(1 for a in att_list if a.status in ("PRESENT", "HALF_DAY"))
        total_secs = sum((a.accumulated_seconds or 0) for a in att_list)
        hours = round(total_secs / 3600, 2)
        settle = settle_map.get(tech.id)
        results.append({
            "technician_id":    str(tech.id),
            "technician_name":  tech.name,
            "technician_mobile": tech.mobile,
            "group_id":         str(group.id) if group else None,
            "group_name":       group.name if group else None,
            "monthly_salary":   group.monthly_salary or 0 if group else 0,
            "total_bookings":   booking_counts.get(tech.id, 0),
            "attendance_days":  att_days,
            "total_hours_worked": hours,
            "settlement_status": settle.status if settle else None,
            "settlement_id":    str(settle.id) if settle else None,
            "net_salary":       settle.net_salary if settle else None,
        })

    return success_response(data={"technicians": results, "total": len(results)})



@router.get("/preview", summary="Preview technician data before generating salary [Admin]")
async def preview_technician_data(
    technician_id: str,
    month: int = Query(..., ge=1, le=12),
    year: int  = Query(...),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns bookings, attendance, cash collections, and revenue for the given
    technician + month/year. Used to review data before generating salary.
    """
    from app.models.technician import Technician
    from app.models.commission import CommissionGroup, CommissionGroupAssignment
    from app.models.booking import Booking
    from app.models.attendance import Attendance
    from app.models.payment import PaymentTransaction, CashCollectionRecord
    from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
    from datetime import date

    tid = UUID(technician_id)
    tech, group = await _get_salary_tech(tid, db)

    sd = date(year, month, 1)
    ed = date(year, month, calendar.monthrange(year, month)[1])
    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    # Bookings
    bookings = (await db.execute(
        select(Booking).where(
            Booking.technician_id == tid,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        ).order_by(Booking.created_at.desc())
    )).scalars().all()

    booking_ids = [b.id for b in bookings]

    # Attendance
    att_rows = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == tid,
            Attendance.date >= sd,
            Attendance.date <= ed,
        ).order_by(Attendance.date)
    )).scalars().all()

    # Payment transactions
    pay_rows = []
    cash_collections = []
    if booking_ids:
        pay_rows = (await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.booking_id.in_(booking_ids),
                PaymentTransaction.status == "SUCCESS",
            ).order_by(PaymentTransaction.created_at.desc())
        )).scalars().all()

        cash_collections = (await db.execute(
            select(CashCollectionRecord).where(
                CashCollectionRecord.booking_id.in_(booking_ids),
            ).order_by(CashCollectionRecord.created_at.desc())
        )).scalars().all()

    # Revenue summary
    cash_total     = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) in ("CASH", "PaymentMethod.CASH"))
    online_total   = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) not in ("CASH", "PaymentMethod.CASH", "PAY_LATER", "PaymentMethod.PAY_LATER"))
    paylater_total = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) in ("PAY_LATER", "PaymentMethod.PAY_LATER"))
    total_revenue  = sum(p.amount or 0 for p in pay_rows)

    att_days   = sum(1 for a in att_rows if a.status in ("PRESENT", "HALF_DAY"))
    total_secs = sum((a.accumulated_seconds or 0) for a in att_rows)
    hours      = round(total_secs / 3600, 2)

    # Status breakdown for bookings
    status_counts: dict = {}
    for b in bookings:
        st = b.status.value if hasattr(b.status, "value") else str(b.status)
        status_counts[st] = status_counts.get(st, 0) + 1

    return success_response(data={
        "technician": {
            "id":     str(tech.id),
            "name":   tech.name,
            "mobile": tech.mobile,
        },
        "group": {
            "name":           group.name,
            "monthly_salary": group.monthly_salary or 0,
            "petrol_amount":  group.petrol_amount  or 0,
            "mobile_recharge": group.mobile_recharge or 0,
            "bonus_amount":   group.bonus_amount   or 0,
            "hra_amount":     group.hra_amount      or 0,
            "other_allowances": group.other_allowances or 0,
        },
        "summary": {
            "total_bookings":    len(bookings),
            "status_breakdown":  status_counts,
            "attendance_days":   att_days,
            "total_hours_worked": hours,
            "cash_in_hand_total": round(sum(c.amount or 0 for c in cash_collections), 2),
            "cash_in_hand_count": len(cash_collections),
            "revenue_cash":      round(cash_total, 2),
            "revenue_online":    round(online_total, 2),
            "revenue_pay_later": round(paylater_total, 2),
            "revenue_total":     round(total_revenue, 2),
        },
        "bookings": [
            {
                "id":             str(b.id),
                "booking_number": b.booking_number if hasattr(b, "booking_number") else None,
                "status":         b.status.value if hasattr(b.status, "value") else str(b.status),
                "service_name":   b.service_name,
                "city":           b.city,
                "total_amount":   b.total_amount,
                "scheduled_date": b.scheduled_date.strftime("%Y-%m-%d") if b.scheduled_date else None,
                "created_at":     iso(b.created_at),
            }
            for b in bookings
        ],
        "attendance": [
            {
                "date":         str(a.date),
                "status":       a.status,
                "hours_worked": round((a.accumulated_seconds or 0) / 3600, 2),
                "check_in":     str(a.check_in)  if a.check_in  else None,
                "check_out":    str(a.check_out) if a.check_out else None,
                "notes":        a.notes,
            }
            for a in att_rows
        ],
        "cash_collections": [
            {
                "id":           str(c.id),
                "booking_id":   str(c.booking_id),
                "amount":       c.amount,
                "status":       c.status.value if hasattr(c.status, "value") else str(c.status),
                "collected_at": iso(c.collected_at) if c.collected_at else None,
                "created_at":   iso(c.created_at),
                "notes":        c.notes,
            }
            for c in cash_collections
        ],
        "revenue_transactions": [
            {
                "id":                str(p.id),
                "booking_id":        str(p.booking_id),
                "method":            p.method.value if hasattr(p.method, "value") else str(p.method),
                "amount":            p.amount,
                "transaction_number": p.transaction_number,
                "reference_number":  p.reference_number,
                "created_at":        iso(p.created_at),
            }
            for p in pay_rows
        ],
    })


@router.post("/generate", summary="Generate/preview salary settlement for a technician [Admin]")
async def generate_salary_settlement(
    payload: GenerateSalaryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates or overwrites a salary settlement record for the given technician + month/year.
    Calculates market reimbursement from commissions (PURCHASE_REIMBURSEMENT).
    """
    from app.models.commission import SalarySettlement, Commission
    from app.models.booking import Booking, BookingStatus
    from app.models.attendance import Attendance
    from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
    from datetime import date

    tid = UUID(payload.technician_id)
    tech, group = await _get_salary_tech(tid, db)

    month = payload.month
    year  = payload.year
    sd = date(year, month, 1)
    ed = date(year, month, calendar.monthrange(year, month)[1])
    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    # Market reimbursement — sum PURCHASE_REIMBURSEMENT commissions in range
    reimb_rows = (await db.execute(
        select(Commission).where(
            Commission.technician_id == tid,
            Commission.item_type == "PURCHASE_REIMBURSEMENT",
            Commission.status.in_(["PENDING", "APPROVED", "PAID"]),
            Commission.created_at >= start_dt,
            Commission.created_at <= end_dt,
        )
    )).scalars().all()
    market_reimb = sum(c.commission_amount or 0 for c in reimb_rows)

    # Booking count
    bk_count = (await db.execute(
        select(func.count(Booking.id)).where(
            Booking.technician_id == tid,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        )
    )).scalar() or 0

    # Attendance
    att_rows = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == tid,
            Attendance.date >= sd,
            Attendance.date <= ed,
        )
    )).scalars().all()
    att_days  = sum(1 for a in att_rows if a.status in ("PRESENT", "HALF_DAY"))
    total_secs = sum((a.accumulated_seconds or 0) for a in att_rows)
    hours = round(total_secs / 3600, 2)

    # Salary components — admin override or group default
    monthly_salary   = payload.monthly_salary   if payload.monthly_salary   is not None else (group.monthly_salary   or 0)
    petrol_amount    = payload.petrol_amount    if payload.petrol_amount    is not None else (group.petrol_amount    or 0)
    mobile_recharge  = payload.mobile_recharge  if payload.mobile_recharge  is not None else (group.mobile_recharge  or 0)
    bonus_amount     = payload.bonus_amount     if payload.bonus_amount     is not None else (group.bonus_amount     or 0)
    hra_amount       = payload.hra_amount       if payload.hra_amount       is not None else (group.hra_amount       or 0)
    other_allowances = payload.other_allowances if payload.other_allowances is not None else (group.other_allowances or 0)
    deductions       = payload.deductions or 0

    gross_salary = monthly_salary + petrol_amount + mobile_recharge + bonus_amount + hra_amount + other_allowances
    net_salary   = gross_salary - deductions + market_reimb

    # Check if existing settlement
    existing = (await db.execute(
        select(SalarySettlement).where(
            SalarySettlement.technician_id == tid,
            SalarySettlement.month == month,
            SalarySettlement.year == year,
        )
    )).scalar_one_or_none()

    if existing and existing.status in ("PAID", "SENT_TO_BANK"):
        raise HTTPException(400, f"Settlement already {existing.status} — cannot regenerate")

    if existing:
        # Update existing
        existing.commission_group_id  = group.id
        existing.monthly_salary       = monthly_salary
        existing.petrol_amount        = petrol_amount
        existing.mobile_recharge      = mobile_recharge
        existing.bonus_amount         = bonus_amount
        existing.hra_amount           = hra_amount
        existing.other_allowances     = other_allowances
        existing.deductions           = deductions
        existing.deduction_notes      = payload.deduction_notes
        existing.market_reimbursement = market_reimb
        existing.gross_salary         = gross_salary
        existing.net_salary           = net_salary
        existing.total_bookings       = bk_count
        existing.total_hours_worked   = hours
        existing.attendance_days      = att_days
        existing.admin_notes          = payload.admin_notes
        existing.updated_at           = datetime.now(timezone.utc)
        settle = existing
    else:
        settle = SalarySettlement(
            technician_id        = tid,
            commission_group_id  = group.id,
            month                = month,
            year                 = year,
            monthly_salary       = monthly_salary,
            petrol_amount        = petrol_amount,
            mobile_recharge      = mobile_recharge,
            bonus_amount         = bonus_amount,
            hra_amount           = hra_amount,
            other_allowances     = other_allowances,
            deductions           = deductions,
            deduction_notes      = payload.deduction_notes,
            market_reimbursement = market_reimb,
            gross_salary         = gross_salary,
            net_salary           = net_salary,
            total_bookings       = bk_count,
            total_hours_worked   = hours,
            attendance_days      = att_days,
            status               = "GENERATED",
            admin_notes          = payload.admin_notes,
            created_by           = UUID(current_user["user_id"]),
        )
        db.add(settle)

    await db.commit()
    await db.refresh(settle)

    return success_response(
        data=_fmt_settlement(settle, tech.name, tech.mobile, group.name),
        message="Salary settlement generated"
    )


@router.get("/{settlement_id}", summary="Get salary settlement details [Admin]")
async def get_settlement(
    settlement_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import SalarySettlement, CommissionGroup
    from app.models.technician import Technician
    from app.models.booking import Booking
    from app.models.attendance import Attendance
    from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
    from datetime import date

    s = (await db.execute(
        select(SalarySettlement).where(SalarySettlement.id == settlement_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Settlement not found")

    tech = (await db.execute(
        select(Technician).where(Technician.id == s.technician_id)  # type: ignore
    )).scalar_one_or_none()

    group = None
    if s.commission_group_id:
        group = (await db.execute(
            select(CommissionGroup).where(CommissionGroup.id == s.commission_group_id)
        )).scalar_one_or_none()

    # Load bookings + attendance for the settlement month/year
    sd = date(s.year, s.month, 1)
    ed = date(s.year, s.month, calendar.monthrange(s.year, s.month)[1])
    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    bookings = (await db.execute(
        select(Booking).where(
            Booking.technician_id == s.technician_id,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        ).order_by(Booking.created_at.desc())
    )).scalars().all()

    att_rows = (await db.execute(
        select(Attendance).where(
            Attendance.technician_id == s.technician_id,
            Attendance.date >= sd,
            Attendance.date <= ed,
        ).order_by(Attendance.date)
    )).scalars().all()

    data = _fmt_settlement(s, tech.name if tech else None, tech.mobile if tech else None, group.name if group else None)
    # ── Enhanced booking details with service name, amount, payment method ──
    from app.models.payment import PaymentTransaction, CashCollectionRecord, CashCollectionStatus
    from app.models.invoice import Invoice

    # Get all booking IDs for this technician in the month
    booking_ids = [b.id for b in bookings]

    # Payment transactions for these bookings (revenue data)
    pay_rows = []
    cash_collections = []
    if booking_ids:
        pay_rows = (await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.booking_id.in_(booking_ids),
                PaymentTransaction.status == "SUCCESS",
            ).order_by(PaymentTransaction.created_at)
        )).scalars().all()

        cash_collections = (await db.execute(
            select(CashCollectionRecord).where(
                CashCollectionRecord.booking_id.in_(booking_ids),
            ).order_by(CashCollectionRecord.created_at.desc())
        )).scalars().all()

    # Revenue summary
    cash_total    = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) in ("CASH", "PaymentMethod.CASH"))
    online_total  = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) not in ("CASH", "PaymentMethod.CASH", "PAY_LATER", "PaymentMethod.PAY_LATER"))
    paylater_total = sum(p.amount or 0 for p in pay_rows if p.method and str(p.method) in ("PAY_LATER", "PaymentMethod.PAY_LATER"))
    total_revenue  = sum(p.amount or 0 for p in pay_rows)

    data["bookings"] = [
        {
            "id": str(b.id),
            "booking_number": b.booking_number if hasattr(b, "booking_number") else None,
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "service_name": b.service_name,
            "city": b.city,
            "total_amount": b.total_amount,
            "created_at": iso(b.created_at),
            "scheduled_date": b.scheduled_date.strftime("%Y-%m-%d") if b.scheduled_date else None,
        }
        for b in bookings
    ]
    data["attendance"] = [
        {
            "date": str(a.date),
            "status": a.status,
            "hours_worked": round((a.accumulated_seconds or 0) / 3600, 2),
            "check_in": str(a.check_in) if a.check_in else None,
            "check_out": str(a.check_out) if a.check_out else None,
            "notes": a.notes,
        }
        for a in att_rows
    ]
    data["cash_collections"] = [
        {
            "id": str(c.id),
            "booking_id": str(c.booking_id),
            "amount": c.amount,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
            "collected_at": iso(c.collected_at) if c.collected_at else None,
            "created_at": iso(c.created_at),
            "notes": c.notes,
        }
        for c in cash_collections
    ]
    data["revenue_summary"] = {
        "cash_total": round(cash_total, 2),
        "online_total": round(online_total, 2),
        "pay_later_total": round(paylater_total, 2),
        "total_revenue": round(total_revenue, 2),
        "total_transactions": len(pay_rows),
    }
    data["revenue_transactions"] = [
        {
            "id": str(p.id),
            "booking_id": str(p.booking_id),
            "method": p.method.value if hasattr(p.method, "value") else str(p.method),
            "amount": p.amount,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "created_at": iso(p.created_at),
            "transaction_number": p.transaction_number,
            "reference_number": p.reference_number,
        }
        for p in pay_rows
    ]
    data["group_structure"] = {
        "monthly_salary":   group.monthly_salary   if group else 0,
        "petrol_amount":    group.petrol_amount    if group else 0,
        "mobile_recharge":  group.mobile_recharge  if group else 0,
        "bonus_amount":     group.bonus_amount     if group else 0,
        "hra_amount":       group.hra_amount       if group else 0,
        "other_allowances": group.other_allowances if group else 0,
        "salary_notes":     group.salary_notes     if group else None,
    } if group else None

    return success_response(data=data)


@router.patch("/{settlement_id}", summary="Update salary settlement amounts [Admin]")
async def update_settlement(
    settlement_id: UUID,
    payload: UpdateSalaryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import SalarySettlement

    s = (await db.execute(
        select(SalarySettlement).where(SalarySettlement.id == settlement_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Settlement not found")
    if s.status in ("PAID", "SENT_TO_BANK"):
        raise HTTPException(400, f"Settlement is {s.status} — cannot update")

    if payload.monthly_salary   is not None: s.monthly_salary   = payload.monthly_salary
    if payload.petrol_amount    is not None: s.petrol_amount    = payload.petrol_amount
    if payload.mobile_recharge  is not None: s.mobile_recharge  = payload.mobile_recharge
    if payload.bonus_amount     is not None: s.bonus_amount     = payload.bonus_amount
    if payload.hra_amount       is not None: s.hra_amount       = payload.hra_amount
    if payload.other_allowances is not None: s.other_allowances = payload.other_allowances
    if payload.deductions       is not None: s.deductions       = payload.deductions
    if payload.deduction_notes  is not None: s.deduction_notes  = payload.deduction_notes
    if payload.admin_notes      is not None: s.admin_notes      = payload.admin_notes

    # Recompute totals
    s.gross_salary = (s.monthly_salary or 0) + (s.petrol_amount or 0) + (s.mobile_recharge or 0) + (s.bonus_amount or 0) + (s.hra_amount or 0) + (s.other_allowances or 0)
    s.net_salary   = s.gross_salary - (s.deductions or 0) + (s.market_reimbursement or 0)
    s.updated_at   = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(s)
    return success_response(data=_fmt_settlement(s), message="Settlement updated")


@router.post("/{settlement_id}/pay", summary="Send salary to technician wallet [Admin]")
async def pay_salary(
    settlement_id: UUID,
    payload: PaySalaryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Credits net_salary to technician wallet as a SALARY transaction.
    Then marks settlement PAID. Admin can then initiate bank/UPI transfer from wallet.
    """
    from app.models.commission import SalarySettlement
    from app.models.technician import Technician
    from app.models.wallet import Wallet, WalletTransaction

    s = (await db.execute(
        select(SalarySettlement).where(SalarySettlement.id == settlement_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Settlement not found")
    if s.status in ("PAID", "SENT_TO_BANK"):
        raise HTTPException(400, f"Salary already {s.status}")

    tech = (await db.execute(
        select(Technician).where(Technician.id == s.technician_id)
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    # Get or create wallet
    wallet = (await db.execute(
        select(Wallet).where(Wallet.technician_id == tech.id)
    )).scalar_one_or_none()
    if not wallet:
        wallet = Wallet(technician_id=tech.id, user_id=tech.user_id, balance=0.0, total_earned=0.0, total_withdrawn=0.0)
        db.add(wallet)
        await db.flush()

    amount = s.net_salary or 0
    bal_before = wallet.balance or 0

    txn = WalletTransaction(
        wallet_id=wallet.id,
        transaction_type="SALARY",
        amount=amount,
        balance_before=bal_before,
        balance_after=bal_before + amount,
        reference_id=str(s.id),
        description=f"Salary for {calendar.month_name[s.month]} {s.year}",
        status="SUCCESS",
    )
    db.add(txn)
    await db.flush()

    wallet.balance      = bal_before + amount
    wallet.total_earned = (wallet.total_earned or 0) + amount

    s.status            = "PAID"
    s.paid_at           = datetime.now(timezone.utc)
    s.wallet_txn_id     = txn.id
    s.payout_method     = payload.payout_method
    s.payment_reference = payload.payment_reference
    s.updated_at        = datetime.now(timezone.utc)

    await db.commit()
    return success_response(
        data={
            "settlement_id": str(s.id),
            "status": "PAID",
            "amount_credited": amount,
            "wallet_balance": wallet.balance,
            "wallet_txn_id": str(txn.id),
        },
        message=f"₹{amount:,.2f} credited to {tech.name}'s wallet"
    )


@router.post("/{settlement_id}/send-to-bank", summary="Initiate bank/UPI transfer from wallet [Admin]")
async def send_to_bank(
    settlement_id: UUID,
    payload: PaySalaryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    After salary is in wallet (PAID), create a WithdrawalRequest on behalf of technician
    and mark it APPROVED immediately (admin-initiated payout).
    """
    from app.models.commission import SalarySettlement
    from app.models.technician import Technician
    from app.models.wallet import Wallet, WalletTransaction, WithdrawalRequest

    s = (await db.execute(
        select(SalarySettlement).where(SalarySettlement.id == settlement_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Settlement not found")
    if s.status != "PAID":
        raise HTTPException(400, "Salary must be credited to wallet first (status=PAID)")

    tech = (await db.execute(
        select(Technician).where(Technician.id == s.technician_id)
    )).scalar_one_or_none()

    wallet = (await db.execute(
        select(Wallet).where(Wallet.technician_id == tech.id)
    )).scalar_one_or_none()
    if not wallet or (wallet.balance or 0) < (s.net_salary or 0):
        raise HTTPException(400, "Insufficient wallet balance for transfer")

    amount = wallet.balance  # Transfer full wallet balance (salary + any reimbursement)

    bal_before = wallet.balance
    debit_txn = WalletTransaction(
        wallet_id=wallet.id,
        transaction_type="WITHDRAWAL",
        amount=amount,
        balance_before=bal_before,
        balance_after=0.0,
        reference_id=str(s.id),
        description=f"Salary bank transfer {calendar.month_name[s.month]} {s.year}",
        status="SUCCESS",
    )
    db.add(debit_txn)
    await db.flush()

    wr = WithdrawalRequest(
        technician_id=tech.id,
        wallet_id=wallet.id,
        amount=amount,
        status="APPROVED",
        upi_id=payload.payment_reference if payload.payout_method == "UPI" else None,
        bank_account=payload.payment_reference if payload.payout_method == "BANK" else None,
        notes=f"Admin-initiated salary transfer for {calendar.month_name[s.month]} {s.year}",
        admin_notes=f"Auto-approved salary settlement {settlement_id}",
        reviewed_by=UUID(current_user["user_id"]),
        reviewed_at=datetime.now(timezone.utc),
        wallet_txn_id=debit_txn.id,
        payment_reference=payload.payment_reference,
    )
    db.add(wr)
    await db.flush()

    wallet.balance        = 0.0
    wallet.total_withdrawn = (wallet.total_withdrawn or 0) + amount

    s.status            = "SENT_TO_BANK"
    s.payment_reference = payload.payment_reference
    s.payout_method     = payload.payout_method
    s.updated_at        = datetime.now(timezone.utc)

    await db.commit()
    return success_response(
        data={
            "settlement_id":    str(s.id),
            "status":           "SENT_TO_BANK",
            "amount_transferred": amount,
            "withdrawal_id":    str(wr.id),
        },
        message=f"₹{amount:,.2f} transfer initiated for {tech.name}"
    )


@router.get("/technician/{technician_id}/wallet-info", summary="Get technician wallet info for salary payout [Admin]")
async def get_tech_wallet_info(
    technician_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.technician import Technician
    from app.models.wallet import Wallet, WithdrawalRequest, WalletTransaction

    tech = (await db.execute(
        select(Technician).where(Technician.id == technician_id)
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    wallet = (await db.execute(
        select(Wallet).where(Wallet.technician_id == technician_id)
    )).scalar_one_or_none()

    # Recent salary transactions
    salary_txns = []
    if wallet:
        salary_txns = (await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.transaction_type == "SALARY",
            ).order_by(WalletTransaction.created_at.desc()).limit(5)
        )).scalars().all()

    return success_response(data={
        "technician_id":    str(tech.id),
        "technician_name":  tech.name,
        "technician_mobile": tech.mobile,
        "upi_id":           getattr(tech, "upi_id", None),
        "bank_account":     getattr(tech, "bank_account", None),
        "bank_ifsc":        getattr(tech, "bank_ifsc", None),
        "bank_name":        getattr(tech, "bank_name", None),
        "payout_method":    getattr(tech, "payout_method", None),
        "wallet": {
            "balance":         wallet.balance       if wallet else 0,
            "total_earned":    wallet.total_earned  if wallet else 0,
            "total_withdrawn": wallet.total_withdrawn if wallet else 0,
        } if wallet else None,
        "recent_salary_transactions": [
            {
                "id":          str(t.id),
                "amount":      t.amount,
                "description": t.description,
                "created_at":  iso(t.created_at),
            }
            for t in salary_txns
        ],
    })
