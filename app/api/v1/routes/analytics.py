from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc
from app.utils.timezone import now_ist
from datetime import datetime, timedelta
from app.core.database import get_db
from app.api.deps import AnyStaff
from app.utils.response import success_response, iso

router = APIRouter()


# ── Terminal "paid/closed" statuses — bookings that count as revenue-generating
# The workflow goes: COMPLETED → INVOICE_GENERATED → PAYMENT_PENDING → PAID → CLOSED/SETTLED
# All these terminal statuses represent real jobs that generated revenue.
REVENUE_STATUSES = [
    "COMPLETED", "INVOICE_GENERATED", "PAYMENT_PENDING",
    "PAID", "CLOSED", "SETTLED",
]

# "Active" (in-flight) statuses — bookings that are currently being worked on
ACTIVE_STATUSES = [
    "CONFIRMED", "ASSIGNED", "ACCEPTED", "EN_ROUTE", "ARRIVED",
    "INSPECTING", "IN_PROGRESS", "WORK_STARTED", "WORK_PAUSED",
    "QUOTATION_APPROVED", "TECHNICIAN_ACCEPTED", "PENDING_VERIFICATION",
]

# "Awaiting action" statuses — show in pending/attention-needed count
PENDING_STATUSES = ["PENDING", "RESCHEDULED", "CANCELLATION_REQUESTED"]


@router.get("/dashboard", summary="Dashboard KPIs [Admin]")
async def dashboard_kpis(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking, BookingStatus
    from app.models.customer import Customer
    from app.models.technician import Technician

    now = now_ist()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    since_30 = now - timedelta(days=29)
    since_6m = now - timedelta(days=180)

    # Build revenue-status filter (any terminal paid status)
    def revenue_filter():
        return Booking.status.in_(REVENUE_STATUSES)

    # ── Booking helpers (inline try/except for safety) ────────────────────
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

    total_bookings = await bk_count()
    today_bookings = await bk_count(Booking.created_at >= today)
    week_bookings  = await bk_count(Booking.created_at >= week_start)

    # Pending = PENDING + RESCHEDULED + CANCELLATION_REQUESTED (needs attention)
    pending_bookings = await bk_count(Booking.status.in_(PENDING_STATUSES))

    # Confirmed = confirmed but not yet assigned/en-route
    confirmed_bookings = await bk_count(Booking.status == BookingStatus.CONFIRMED)

    # In-progress = all active in-flight statuses
    in_progress_bookings = await bk_count(Booking.status.in_(ACTIVE_STATUSES))

    # Completed this month = any terminal revenue status created this month
    completed_this_month = await bk_count(
        and_(revenue_filter(), Booking.created_at >= month_start)
    )

    # Cancelled this month
    cancelled_this_month = await bk_count(
        and_(Booking.status == BookingStatus.CANCELLED, Booking.created_at >= month_start)
    )

    # Total completed = all terminal revenue statuses ever
    total_completed = await bk_count(revenue_filter())

    # ── Revenue — sum total_amount for all terminal statuses ──────────────
    total_revenue  = await bk_sum(revenue_filter())
    month_revenue  = await bk_sum(and_(revenue_filter(), Booking.created_at >= month_start))
    week_revenue   = await bk_sum(and_(revenue_filter(), Booking.created_at >= week_start))
    today_revenue  = await bk_sum(and_(revenue_filter(), Booking.created_at >= today))
    prev_month_rev = await bk_sum(and_(
        revenue_filter(),
        Booking.created_at >= prev_month_start,
        Booking.created_at <= prev_month_end,
    ))

    month_growth = round(
        ((month_revenue - prev_month_rev) / prev_month_rev * 100) if prev_month_rev else 0, 1
    )
    completion_rate = round((total_completed / total_bookings * 100) if total_bookings else 0, 1)

    # ── Revenue chart — last 30 days (all revenue statuses) ───────────────
    revenue_chart = []
    try:
        rows = (await db.execute(
            select(
                func.date(Booking.created_at).label("d"),
                func.coalesce(func.sum(Booking.total_amount), 0).label("rev"),
                func.count(Booking.id).label("cnt"),
            )
            .where(and_(revenue_filter(), Booking.created_at >= since_30))
            .group_by(func.date(Booking.created_at))
            .order_by(func.date(Booking.created_at))
        )).all()
        revenue_chart = [{"date": str(r.d), "revenue": int(round(float(r.rev))), "bookings": r.cnt} for r in rows]
    except Exception:
        revenue_chart = []

    # ── Booking status breakdown (last 30 days) — all statuses ───────────
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
    # NOTE: to_char() with GROUP BY fails in asyncpg (parameterized string not
    # recognized as same expression). Use date_trunc('month') + Python formatting.
    monthly_trend = []
    try:
        month_trunc = func.date_trunc("month", Booking.created_at)
        rows = (await db.execute(
            select(
                month_trunc.label("month_dt"),
                func.count(Booking.id).label("total"),
            )
            .where(Booking.created_at >= since_6m)
            .group_by(month_trunc)
            .order_by(month_trunc)
        )).all()
        comp_rows = (await db.execute(
            select(
                month_trunc.label("month_dt"),
                func.count(Booking.id).label("completed"),
            )
            .where(and_(revenue_filter(), Booking.created_at >= since_6m))
            .group_by(month_trunc)
        )).all()
        comp_map = {str(r.month_dt)[:7]: r.completed for r in comp_rows}
        monthly_trend = [
            {"month": str(r.month_dt)[:7], "total": r.total, "completed": comp_map.get(str(r.month_dt)[:7], 0)}
            for r in rows
        ]
    except Exception:
        monthly_trend = []

    # ── Reset transaction after chart queries so remaining KPI queries succeed ──
    try:
        await db.rollback()
    except Exception:
        pass

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

    # ── Top technicians — ranked by live job count from bookings ──────────
    # Since technician.total_jobs may be stale, compute it live from bookings
    top_technicians = []
    try:
        # Live count: how many terminal-status bookings are assigned to each tech
        tech_job_rows = (await db.execute(
            select(
                Booking.technician_id,
                func.count(Booking.id).label("job_count"),
            )
            .where(
                and_(
                    Booking.technician_id.isnot(None),
                    revenue_filter(),
                )
            )
            .group_by(Booking.technician_id)
            .order_by(desc(func.count(Booking.id)))
            .limit(5)
        )).all()

        if tech_job_rows:
            tech_ids = [r.technician_id for r in tech_job_rows]
            job_count_map = {r.technician_id: r.job_count for r in tech_job_rows}
            techs = (await db.execute(
                select(Technician)
                .where(Technician.id.in_(tech_ids))
            )).scalars().all()
            tech_map = {t.id: t for t in techs}
            top_technicians = [
                {
                    "id": str(tid),
                    "name": tech_map[tid].name if tid in tech_map else "Unknown",
                    "rating": float(tech_map[tid].rating or 0) if tid in tech_map else 0.0,
                    "total_jobs": job_count_map[tid],
                    "status": tech_map[tid].status if tid in tech_map else "ACTIVE",
                }
                for tid in tech_ids if tid in tech_map
            ]
        else:
            # Fallback: just list active technicians with stored total_jobs
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
                "created_at": iso(bk.created_at) if bk.created_at else None,
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
            "total": int(round(total_revenue)),
            "this_month": int(round(month_revenue)),
            "this_week": int(round(week_revenue)),
            "today": int(round(today_revenue)),
            "prev_month": int(round(prev_month_rev)),
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
    since = now_ist() - timedelta(days=days)
    try:
        result = await db.execute(
            select(
                func.date(Booking.created_at).label("date"),
                func.coalesce(func.sum(Booking.total_amount), 0).label("revenue"),
                func.count(Booking.id).label("count"),
            )
            .where(and_(Booking.status.in_(REVENUE_STATUSES), Booking.created_at >= since))
            .group_by(func.date(Booking.created_at))
            .order_by(func.date(Booking.created_at))
        )
        rows = result.all()
        data = [{"date": str(r.date), "revenue": int(round(float(r.revenue))), "bookings": r.count} for r in rows]
    except Exception:
        data = []
    return success_response(data={"period": period, "data": data})


@router.get("/bookings", summary="Booking analytics [Admin]")
async def booking_analytics(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking, BookingStatus
    since = now_ist() - timedelta(days=30)
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
    since = now_ist() - timedelta(days=30)
    try:
        new_this_month = (await db.execute(
            select(func.count()).select_from(Customer).where(Customer.created_at >= since)
        )).scalar_one()
        total = (await db.execute(select(func.count()).select_from(Customer))).scalar_one()
    except Exception:
        new_this_month, total = 0, 0
    return success_response(data={"total_customers": total, "new_this_month": new_this_month})
