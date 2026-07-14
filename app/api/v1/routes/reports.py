from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
from datetime import date
import sqlalchemy as sa

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyStaff
from app.core.database import get_db
from app.services.reporting import (
    build_customer_report,
    build_gst_report,
    build_placeholder_report,
    build_revenue_report,
)
from app.utils.response import success_response

router = APIRouter()


def _handle_report_range_error(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/revenue", summary="Revenue report")
async def revenue_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    period: str | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    # Convert year/month/period to date range if explicit dates not given
    if not start_date and not end_date and year and month:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)
    try:
        report = await build_revenue_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/gst", summary="GST report")
async def gst_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    # Convert year/month to date range if explicit dates not given
    if not start_date and not end_date and year and month:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)
    try:
        report = await build_gst_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/commission", summary="Commission report")
async def commission_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("commission", "Commission source tables are not implemented yet"),
        message="Commission report is waiting on the commission module",
    )


@router.get("/inventory", summary="Inventory report")
async def inventory_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("inventory", "Inventory source tables are not implemented yet"),
        message="Inventory report is waiting on the inventory module",
    )


@router.get("/amc", summary="AMC report")
async def amc_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("amc", "AMC source tables are not implemented yet"),
        message="AMC report is waiting on the AMC module",
    )


@router.get("/warranty", summary="Warranty report")
async def warranty_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("warranty", "Warranty source tables are not implemented yet"),
        message="Warranty report is waiting on the warranty module",
    )


@router.get("/customer", summary="Customer report")
async def customer_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_customer_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/franchise", summary="Franchise report")
async def franchise_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("franchise", "Franchise source tables are not implemented yet"),
        message="Franchise report is waiting on the franchise module",
    )


@router.get("/technician", summary="Technician performance report")
async def technician_report(
    technician_id: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    period: str = Query("monthly", regex="^(daily|weekly|monthly|yearly)$"),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    """Returns booking counts, revenue, ratings, attendance for one or all technicians."""
    from sqlalchemy import select, func, and_
    from app.models.technician import Technician
    from app.models.booking import Booking
    from app.models.payment import PaymentTransaction, PaymentStatus
    from app.models.attendance import AttendanceRecord
    from uuid import UUID

    # Date range defaults
    from datetime import datetime, timezone
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        from dateutil.relativedelta import relativedelta
        start_date = end_date - relativedelta(months=1)

    start_dt = ist_midnight_utc(start_date)
    end_dt = ist_end_of_day_utc(end_date)

    # Base technician query
    tech_q = select(Technician).where(Technician.is_active == True)
    if technician_id:
        try:
            tech_q = tech_q.where(Technician.id == UUID(technician_id))
        except Exception:
            pass
    technicians = (await db.execute(tech_q)).scalars().all()

    results = []
    for tech in technicians:
        # Booking stats
        booking_q = select(
            func.count(Booking.id).label("total"),
            func.sum(
                func.cast(Booking.status == "COMPLETED", sa.Integer)
            ).label("completed"),
        ).where(
            Booking.technician_id == tech.id,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        )
        bk_row = (await db.execute(booking_q)).one()
        total_bookings = bk_row.total or 0
        completed = int(bk_row.completed or 0)

        # Revenue
        rev_q = select(func.sum(PaymentTransaction.amount)).join(
            Booking, Booking.id == PaymentTransaction.booking_id
        ).where(
            Booking.technician_id == tech.id,
            PaymentTransaction.status == PaymentStatus.SUCCESS,
            PaymentTransaction.paid_at >= start_dt,
            PaymentTransaction.paid_at <= end_dt,
        )
        revenue = (await db.execute(rev_q)).scalar_one() or 0.0

        results.append({
            "technician_id": str(tech.id),
            "technician_name": tech.name,
            "mobile": tech.mobile,
            "total_bookings": total_bookings,
            "completed_bookings": completed,
            "completion_rate": round((completed / total_bookings * 100) if total_bookings else 0, 1),
            "revenue_generated": round(revenue, 2),
            "period": {"start": str(start_date), "end": str(end_date)},
        })

    results.sort(key=lambda x: x["revenue_generated"], reverse=True)
    return success_response(data={
        "technicians": results,
        "period": {"start": str(start_date), "end": str(end_date)},
        "total_technicians": len(results),
    })


# ── GET /reports/technician-detail ──────────────────────────────────────────
@router.get("/technician-detail", summary="Full technician report [Admin]")
async def technician_detail_report(
    technician_id: str = Query(..., description="Technician UUID"),
    period: str = Query("monthly", regex="^(weekly|monthly|yearly)$"),
    year: int = Query(None),
    month: int = Query(None),
    week: int = Query(None),          # ISO week number (1-53); used when period=weekly
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a full, detailed report for a single technician covering the
    requested time window:

    • Summary KPIs — total / completed / cancelled bookings, revenue,
      cash vs online split, avg rating, completion rate
    • Booking list with status, quotation number, invoice number, amount
    • Payment breakdown (CASH vs ONLINE/RAZORPAY totals)
    • Quotation list with status
    • Rating list from customers
    """
    import calendar
    from datetime import datetime, timedelta
    from uuid import UUID
    from sqlalchemy import select, func, case, and_, or_
    from app.models.technician import Technician, TechnicianRating
    from app.models.booking import Booking, BookingStatus
    from app.models.quotation import Quotation
    from app.models.invoice import Invoice
    from app.models.payment import PaymentTransaction, PaymentStatus, PaymentMethod
    from app.models.commission import Commission

    # ── Resolve date range ─────────────────────────────────────────────────
    today = date.today()

    if start_date and end_date:
        sd, ed = start_date, end_date
    elif period == "weekly":
        y = year or today.isocalendar()[0]
        w = week or today.isocalendar()[1]
        # ISO week: Monday = day 1
        jan4 = date(y, 1, 4)
        week_start = jan4 + timedelta(weeks=w - 1, days=-jan4.weekday())
        sd = week_start
        ed = week_start + timedelta(days=6)
    elif period == "monthly":
        y = year or today.year
        m = month or today.month
        sd = date(y, m, 1)
        ed = date(y, m, calendar.monthrange(y, m)[1])
    else:  # yearly
        y = year or today.year
        sd = date(y, 1, 1)
        ed = date(y, 12, 31)

    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    # ── Fetch technician ───────────────────────────────────────────────────
    try:
        tid = UUID(technician_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid technician_id")

    tech = (await db.execute(
        select(Technician).where(Technician.id == tid)
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(status_code=404, detail="Technician not found")

    # ── Bookings in range ──────────────────────────────────────────────────
    bookings = (await db.execute(
        select(Booking).where(
            Booking.technician_id == tid,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        ).order_by(Booking.created_at.desc())
    )).scalars().all()

    booking_ids = [b.id for b in bookings]

    # ── Quotations for those bookings ──────────────────────────────────────
    quotations = []
    if booking_ids:
        quotations = (await db.execute(
            select(Quotation).where(Quotation.booking_id.in_(booking_ids))
        )).scalars().all()

    quot_by_booking: dict = {}
    for q in quotations:
        quot_by_booking.setdefault(q.booking_id, []).append(q)

    # ── Invoices for those bookings ────────────────────────────────────────
    invoices = []
    if booking_ids:
        invoices = (await db.execute(
            select(Invoice).where(Invoice.booking_id.in_(booking_ids))
        )).scalars().all()

    inv_by_booking: dict = {}
    for inv in invoices:
        inv_by_booking[inv.booking_id] = inv

    # ── Payments for those bookings ────────────────────────────────────────
    payments = []
    if booking_ids:
        payments = (await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.booking_id.in_(booking_ids),
                PaymentTransaction.status == PaymentStatus.SUCCESS,
            )
        )).scalars().all()

    pay_by_booking: dict = {}
    for p in payments:
        pay_by_booking.setdefault(p.booking_id, []).append(p)

    # ── Ratings given to this technician ──────────────────────────────────
    ratings_rows = (await db.execute(
        select(TechnicianRating).where(
            TechnicianRating.technician_id == tid,
            TechnicianRating.created_at >= start_dt,
            TechnicianRating.created_at <= end_dt,
        ).order_by(TechnicianRating.created_at.desc())
    )).scalars().all()

    # ── Commissions for this technician in range ────────────────────────────
    commission_rows = (await db.execute(
        select(Commission).where(
            Commission.technician_id == tid,
            Commission.created_at >= start_dt,
            Commission.created_at <= end_dt,
        ).order_by(Commission.created_at.desc())
    )).scalars().all()

    # ── Aggregate KPIs ─────────────────────────────────────────────────────
    total_bookings     = len(bookings)
    completed_bookings = sum(1 for b in bookings if b.status in (
        BookingStatus.COMPLETED, BookingStatus.PAID,
        BookingStatus.CLOSED, BookingStatus.SETTLED,
        BookingStatus.PAYMENT_PENDING, BookingStatus.INVOICE_GENERATED,
    ))
    cancelled_bookings = sum(1 for b in bookings if b.status == BookingStatus.CANCELLED)
    active_bookings    = total_bookings - completed_bookings - cancelled_bookings

    total_cash   = sum(p.amount for p in payments if p.method == PaymentMethod.CASH)
    total_online = sum(p.amount for p in payments if p.method == PaymentMethod.RAZORPAY)
    total_revenue = total_cash + total_online

    avg_rating = (
        round(sum(r.rating for r in ratings_rows) / len(ratings_rows), 2)
        if ratings_rows else None
    )

    # Commission aggregates
    total_commission_earned  = sum(c.commission_amount or 0 for c in commission_rows)
    total_commission_pending = sum(c.commission_amount or 0 for c in commission_rows if c.status == "PENDING")
    total_commission_paid    = sum(c.commission_amount or 0 for c in commission_rows if c.status == "PAID")
    total_commission_approved = sum(c.commission_amount or 0 for c in commission_rows if c.status == "APPROVED")

    # ── Build booking rows ─────────────────────────────────────────────────
    def _iso(dt):
        return dt.isoformat() if dt else None

    booking_rows = []
    for b in bookings:
        q_list  = quot_by_booking.get(b.id, [])
        inv     = inv_by_booking.get(b.id)
        p_list  = pay_by_booking.get(b.id, [])

        # Pick the most advanced quotation
        quot = None
        if q_list:
            _order = ['APPROVED', 'SUBMITTED', 'DRAFT', 'REJECTED']
            for s in _order:
                found = next((q for q in q_list if q.status.value == s), None)
                if found:
                    quot = found
                    break
            if quot is None:
                quot = q_list[0]

        paid_cash   = sum(p.amount for p in p_list if p.method == PaymentMethod.CASH)
        paid_online = sum(p.amount for p in p_list if p.method == PaymentMethod.RAZORPAY)

        booking_rows.append({
            "booking_id":       str(b.id),
            "booking_number":   b.booking_number,
            "service_name":     b.service_name if hasattr(b, 'service_name') else None,
            "status":           b.status.value,
            "scheduled_date":   str(b.scheduled_date) if b.scheduled_date else None,
            "total_amount":     b.total_amount or 0.0,
            "cancelled_reason": b.cancelled_reason,
            "created_at":       _iso(b.created_at),
            "quotation_number": quot.quotation_number if quot else None,
            "quotation_status": quot.status.value if quot else None,
            "invoice_number":   inv.invoice_number if inv else None,
            "invoice_total":    inv.total_amount if inv else None,
            "invoice_status":   inv.status.value if inv else None,
            "paid_cash":        paid_cash,
            "paid_online":      paid_online,
            "paid_total":       paid_cash + paid_online,
        })

    # ── Build rating rows ──────────────────────────────────────────────────
    rating_rows = [{
        "rating":     r.rating,
        "review":     r.review if hasattr(r, 'review') else None,
        "booking_id": str(r.booking_id) if r.booking_id else None,
        "created_at": _iso(r.created_at),
    } for r in ratings_rows]

    # ── Build quotation summary rows ───────────────────────────────────────
    quot_rows = [{
        "quotation_number": q.quotation_number,
        "booking_id":       str(q.booking_id),
        "status":           q.status.value,
        "total_amount":     q.total_amount if hasattr(q, 'total_amount') else None,
        "created_at":       _iso(q.created_at),
    } for q in quotations]

    # ── Build commission rows ───────────────────────────────────────────────
    commission_detail_rows = [{
        "id":                str(c.id),
        "booking_id":        str(c.booking_id) if c.booking_id else None,
        "item_type":         c.item_type,
        "item_name":         c.item_name,
        "base_amount":       c.base_amount,
        "commission_amount": c.commission_amount,
        "status":            c.status,
        "payout_date":       _iso(c.payout_date),
        "part_source":       c.part_source,
        "notes":             c.notes,
        "created_at":        _iso(c.created_at),
    } for c in commission_rows]

    return success_response(data={
        "technician": {
            "id":      str(tech.id),
            "name":    tech.name,
            "mobile":  tech.mobile,
            "email":   tech.email,
            "city":    tech.city,
            "rating":  tech.rating,
            "profile_image": tech.profile_image,
        },
        "period": {
            "type":       period,
            "start_date": str(sd),
            "end_date":   str(ed),
        },
        "summary": {
            "total_bookings":     total_bookings,
            "completed_bookings": completed_bookings,
            "cancelled_bookings": cancelled_bookings,
            "active_bookings":    active_bookings,
            "completion_rate":    round((completed_bookings / total_bookings * 100) if total_bookings else 0, 1),
            "total_revenue":      round(total_revenue, 2),
            "total_cash":         round(total_cash, 2),
            "total_online":       round(total_online, 2),
            "avg_rating":         avg_rating,
            "total_ratings":      len(ratings_rows),
            "total_quotations":   len(quotations),
            "total_invoices":     len(invoices),
        },
        "bookings":     booking_rows,
        "quotations":   quot_rows,
        "ratings":      rating_rows,
        "commissions":  commission_detail_rows,
        "commission_summary": {
            "total_earned":   round(total_commission_earned, 2),
            "total_pending":  round(total_commission_pending, 2),
            "total_approved": round(total_commission_approved, 2),
            "total_paid":     round(total_commission_paid, 2),
            "total_records":  len(commission_rows),
        },
    })
