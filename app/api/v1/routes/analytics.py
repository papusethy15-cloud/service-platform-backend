from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, desc, text
from datetime import datetime, timedelta
from app.core.database import get_db
from app.api.deps import AnyStaff
from app.utils.response import success_response

router = APIRouter()



@router.get("/dashboard", summary="Dashboard KPIs [Admin]")
async def dashboard_kpis(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking, BookingStatus
    from app.models.customer import Customer
    from app.models.technician import Technician

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    since_30 = now - timedelta(days=29)
    since_6m = now - timedelta(days=180)

    # ── Booking counts ─────────────────────────────────────────────────────
    total_bookings = (await _safe(
        db.execute(select(func.count()).select_from(Booking)), type('R', (), {'scalar_one': lambda self: 0})()
    )).scalar_one()

    # Simpler scalar helpers
    async def bk_count(where_clause=None):
        q = select(func.count()).select_from(Booking)
        if where_clause is not None:
            q = q.where(where_clause)
        try:
            return (await db.execute(q)).scalar_one()
        except Exception:
            return 0

    async def bk_sum(where_clause=None):
        q = select(func.coalesce(func.sum(Booking.total_amount), 0))
        if where_clause is not None:
            q = q.where(where_clause)
        try:
            return float((await db.execute(q)).scalar_one())
        except Exception:
            return 0.0

    total_bookings        = await bk_count()
    today_bookings        = await bk_count(Booking.created_at >= today)
    week_bookings         = await bk_count(Booking.created_at >= week_start)
    pending_bookings      = await bk_count(Booking.status == BookingStatus.PENDING)
    confirmed_bookings    = await bk_count(Booking.status == BookingStatus.CONFIRMED)
    in_progress_bookings  = await bk_count(Booking.status == BookingStatus.IN_PROGRESS)
    completed_this_month  = await bk_count(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= month_start))
    cancelled_this_month  = await bk_count(and_(Booking.status == BookingStatus.CANCELLED, Booking.created_at >= month_start))
    total_completed       = await bk_count(Booking.status == BookingStatus.COMPLETED)

    # ── Revenue ────────────────────────────────────────────────────────────
    total_revenue  = await bk_sum(Booking.status == BookingStatus.COMPLETED)
    month_revenue  = await bk_sum(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= month_start))
    week_revenue   = await bk_sum(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= week_start))
    today_revenue  = await bk_sum(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= today))
    prev_month_rev = await bk_sum(and_(
        Booking.status == BookingStatus.COMPLETED,
        Booking.created_at >= prev_month_start,
        Booking.created_at <= prev_month_end,
    ))

    month_growth = round(
        ((month_revenue - prev_month_rev) / prev_month_rev * 100) if prev_month_rev else 0, 1
    )
    completion_rate = round((total_completed / total_bookings * 100) if total_bookings else 0, 1)

    # ── Revenue chart — last 30 days ───────────────────────────────────────
    revenue_chart = []
    try:
        rows = (await db.execute(
            select(
                func.date(Booking.created_at).label("d"),
                func.coalesce(func.sum(Booking.total_amount), 0).label("rev"),
                func.count(Booking.id).label("cnt"),
            )
            .where(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= since_30))
            .group_by(func.date(Booking.created_at))
            .order_by(func.date(Booking.created_at))
        )).all()
        revenue_chart = [{"date": str(r.d), "revenue": round(float(r.rev), 2), "bookings": r.cnt} for r in rows]
    except Exception:
        revenue_chart = []

    # ── Booking status breakdown (last 30 days) ───────────────────────────
    status_chart = {}
    try:
        rows = (await db.execute(
            select(Booking.status, func.count(Booking.id))
            .where(Booking.created_at >= since_30)
            .group_by(Booking.status)
        )).all()
        status_chart = {s.value: c for s, c in rows}
    except Exception:
        status_chart = {}

    # ── Monthly booking trend — last 6 months ─────────────────────────────
    monthly_trend = []
    try:
        rows = (await db.execute(
            select(
                func.to_char(Booking.created_at, "YYYY-MM").label("month"),
                func.count(Booking.id).label("total"),
            )
            .where(Booking.created_at >= since_6m)
            .group_by(func.to_char(Booking.created_at, "YYYY-MM"))
            .order_by(func.to_char(Booking.created_at, "YYYY-MM"))
        )).all()
        # Get completed per month separately to avoid complex join
        comp_rows = (await db.execute(
            select(
                func.to_char(Booking.created_at, "YYYY-MM").label("month"),
                func.count(Booking.id).label("completed"),
            )
            .where(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= since_6m))
            .group_by(func.to_char(Booking.created_at, "YYYY-MM"))
        )).all()
        comp_map = {r.month: r.completed for r in comp_rows}
        monthly_trend = [{"month": r.month, "total": r.total, "completed": comp_map.get(r.month, 0)} for r in rows]
    except Exception:
        monthly_trend = []

    # ── Customer KPIs ──────────────────────────────────────────────────────
    async def cust_count(where_clause=None):
        q = select(func.count()).select_from(Customer)
        if where_clause is not None:
            q = q.where(where_clause)
        try:
            return (await db.execute(q)).scalar_one()
        except Exception:
            return 0

    total_customers      = await cust_count()
    new_customers_month  = await cust_count(Customer.created_at >= month_start)
    new_customers_today  = await cust_count(Customer.created_at >= today)

    # ── Technician KPIs ───────────────────────────────────────────────────
    async def tech_count(where_clause=None):
        q = select(func.count()).select_from(Technician)
        if where_clause is not None:
            q = q.where(where_clause)
        try:
            return (await db.execute(q)).scalar_one()
        except Exception:
            return 0

    active_techs = await tech_count(and_(Technician.is_active == True, Technician.status == "ACTIVE"))
    total_techs  = await tech_count(Technician.is_active == True)

    # ── Top technicians ───────────────────────────────────────────────────
    top_technicians = []
    try:
        techs = (await db.execute(
            select(Technician)
            .where(Technician.is_active == True)
            .order_by(desc(Technician.total_jobs), desc(Technician.rating))
            .limit(5)
        )).scalars().all()
        top_technicians = [
            {
                "id": str(t.id),
                "name": t.name,
                "rating": float(t.rating or 0),
                "total_jobs": int(t.total_jobs or 0),
                "status": t.status,
            }
            for t in techs
        ]
    except Exception:
        top_technicians = []

    # ── Recent bookings ───────────────────────────────────────────────────
    recent_bookings = []
    try:
        bk_rows = (await db.execute(
            select(Booking, Customer.name.label("cname"))
            .outerjoin(Customer, Customer.id == Booking.customer_id)
            .order_by(desc(Booking.created_at))
            .limit(8)
        )).all()
        recent_bookings = [
            {
                "id": str(bk.id),
                "booking_number": bk.booking_number,
                "customer_name": cname or "—",
                "status": bk.status.value if bk.status else "UNKNOWN",
                "total_amount": float(bk.total_amount or 0),
                "created_at": bk.created_at.isoformat() if bk.created_at else None,
            }
            for bk, cname in bk_rows
        ]
    except Exception:
        recent_bookings = []

    # ── Open escalations ──────────────────────────────────────────────────
    open_escalations = 0
    try:
        from app.models.escalation import Escalation, EscalationStatus
        open_escalations = (await db.execute(
            select(func.count()).select_from(Escalation)
            .where(Escalation.status == EscalationStatus.OPEN)
        )).scalar_one()
    except Exception:
        open_escalations = 0

    return success_response(data={
        "bookings": {
            "total": total_bookings,
            "today": today_bookings,
            "this_week": week_bookings,
            "pending": pending_bookings,
            "confirmed": confirmed_bookings,
            "in_progress": in_progress_bookings,
            "completed_this_month": completed_this_month,
            "cancelled_this_month": cancelled_this_month,
            "total_completed": total_completed,
            "completion_rate": completion_rate,
        },
        "revenue": {
            "total": round(total_revenue, 2),
            "this_month": round(month_revenue, 2),
            "this_week": round(week_revenue, 2),
            "today": round(today_revenue, 2),
            "prev_month": round(prev_month_rev, 2),
            "month_growth": month_growth,
        },
        "customers": {
            "total": total_customers,
            "new_this_month": new_customers_month,
            "new_today": new_customers_today,
        },
        "technicians": {
            "active": active_techs,
            "total": total_techs,
        },
        "open_escalations": open_escalations,
        "charts": {
            "revenue_last_30_days": revenue_chart,
            "booking_status": status_chart,
            "monthly_trend": monthly_trend,
        },
        "top_technicians": top_technicians,
        "recent_bookings": recent_bookings,
    })


@router.get("/revenue", summary="Revenue analytics [Admin]")
async def revenue_analytics(
    period: str = Query("monthly", regex="^(daily|weekly|monthly)$"),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking, BookingStatus
    days = 30 if period == "monthly" else (7 if period == "weekly" else 14)
    since = datetime.utcnow() - timedelta(days=days)
    try:
        result = await db.execute(
            select(
                func.date(Booking.created_at).label("date"),
                func.coalesce(func.sum(Booking.total_amount), 0).label("revenue"),
                func.count(Booking.id).label("count"),
            )
            .where(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= since))
            .group_by(func.date(Booking.created_at))
            .order_by(func.date(Booking.created_at))
        )
        rows = result.all()
        data = [{"date": str(r.date), "revenue": round(float(r.revenue), 2), "bookings": r.count} for r in rows]
    except Exception:
        data = []
    return success_response(data={"period": period, "data": data})


@router.get("/bookings", summary="Booking analytics [Admin]")
async def booking_analytics(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking, BookingStatus
    since = datetime.utcnow() - timedelta(days=30)
    try:
        rows = (await db.execute(
            select(Booking.status, func.count(Booking.id))
            .where(Booking.created_at >= since)
            .group_by(Booking.status)
        )).all()
        by_status = {s.value: c for s, c in rows}
    except Exception:
        by_status = {}
    return success_response(data={"by_status": by_status, "period": "last_30_days"})


@router.get("/technicians", summary="Technician analytics [Admin]")
async def technician_analytics(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.technician import Technician
    try:
        techs = (await db.execute(
            select(Technician)
            .where(Technician.is_active == True)
            .order_by(desc(Technician.rating))
            .limit(10)
        )).scalars().all()
        data = [{"id": str(t.id), "name": t.name, "rating": t.rating, "total_jobs": t.total_jobs} for t in techs]
    except Exception:
        data = []
    return success_response(data={"top_technicians": data})


@router.get("/customers", summary="Customer analytics [Admin]")
async def customer_analytics(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.customer import Customer
    since = datetime.utcnow() - timedelta(days=30)
    try:
        new_this_month = (await db.execute(
            select(func.count()).select_from(Customer).where(Customer.created_at >= since)
        )).scalar_one()
        total = (await db.execute(select(func.count()).select_from(Customer))).scalar_one()
    except Exception:
        new_this_month, total = 0, 0
    return success_response(data={"total_customers": total, "new_this_month": new_this_month})
