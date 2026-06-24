from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from datetime import datetime, timedelta
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff
from app.utils.response import success_response

router = APIRouter()

@router.get("/dashboard", summary="Dashboard KPIs [Admin]")
async def dashboard_kpis(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.booking import Booking, BookingStatus
    from app.models.customer import Customer
    from app.models.technician import Technician
    today = datetime.utcnow().replace(hour=0, minute=0, second=0)
    month_start = today.replace(day=1)

    total_bookings   = (await db.execute(select(func.count()).select_from(Booking))).scalar_one()
    today_bookings   = (await db.execute(select(func.count()).select_from(Booking).where(Booking.created_at >= today))).scalar_one()
    pending_bookings = (await db.execute(select(func.count()).select_from(Booking).where(Booking.status == BookingStatus.PENDING))).scalar_one()
    completed_this_month = (await db.execute(select(func.count()).select_from(Booking).where(
        and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= month_start)))).scalar_one()
    total_revenue    = (await db.execute(select(func.coalesce(func.sum(Booking.total_amount), 0)).where(Booking.status == BookingStatus.COMPLETED))).scalar_one()
    month_revenue    = (await db.execute(select(func.coalesce(func.sum(Booking.total_amount), 0)).where(
        and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= month_start)))).scalar_one()
    total_customers  = (await db.execute(select(func.count()).select_from(Customer))).scalar_one()
    active_techs     = (await db.execute(select(func.count()).select_from(Technician).where(Technician.status == "ACTIVE"))).scalar_one()

    return success_response(data={
        "bookings": {"total": total_bookings, "today": today_bookings,
                     "pending": pending_bookings, "completed_this_month": completed_this_month},
        "revenue":  {"total": round(float(total_revenue), 2), "this_month": round(float(month_revenue), 2)},
        "customers": {"total": total_customers},
        "technicians": {"active": active_techs},
    })

@router.get("/revenue", summary="Revenue analytics [Admin]")
async def revenue_analytics(
    period: str = Query("monthly", regex="^(daily|weekly|monthly)$"),
    current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)
):
    from app.models.booking import Booking, BookingStatus
    days = 30 if period == "monthly" else (7 if period == "weekly" else 14)
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(func.date(Booking.created_at).label("date"),
               func.coalesce(func.sum(Booking.total_amount), 0).label("revenue"),
               func.count(Booking.id).label("count"))
        .where(and_(Booking.status == BookingStatus.COMPLETED, Booking.created_at >= since))
        .group_by(func.date(Booking.created_at)).order_by(func.date(Booking.created_at))
    )
    rows = result.all()
    return success_response(data={"period": period, "data": [
        {"date": str(r.date), "revenue": round(float(r.revenue), 2), "bookings": r.count} for r in rows
    ]})

@router.get("/bookings", summary="Booking analytics [Admin]")
async def booking_analytics(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.booking import Booking, BookingStatus
    since = datetime.utcnow() - timedelta(days=30)
    status_counts = (await db.execute(
        select(Booking.status, func.count(Booking.id))
        .where(Booking.created_at >= since).group_by(Booking.status)
    )).all()
    return success_response(data={
        "by_status": {s.value: c for s, c in status_counts},
        "period": "last_30_days"
    })

@router.get("/technicians", summary="Technician analytics [Admin]")
async def technician_analytics(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.technician import Technician
    from app.models.booking import Booking, BookingStatus
    techs = (await db.execute(select(Technician).where(Technician.is_active == True).order_by(Technician.rating.desc()).limit(10))).scalars().all()
    return success_response(data={"top_technicians": [
        {"id": str(t.id), "name": t.name, "rating": t.rating, "total_jobs": t.total_jobs} for t in techs
    ]})

@router.get("/customers", summary="Customer analytics [Admin]")
async def customer_analytics(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.customer import Customer
    since = datetime.utcnow() - timedelta(days=30)
    new_this_month = (await db.execute(select(func.count()).select_from(Customer).where(Customer.created_at >= since))).scalar_one()
    total = (await db.execute(select(func.count()).select_from(Customer))).scalar_one()
    return success_response(data={"total_customers": total, "new_this_month": new_this_month})
