from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminCCOTech, AnyAuthenticated, AnyStaff
from app.api.v1.schemas.tracking import UpdateLocationRequest
from app.core.database import get_db
from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer, CustomerAddress
from app.models.technician import Technician
from app.models.tracking import TrackingLocation
from app.utils.response import success_response

router = APIRouter()


def _serialize_location(location: TrackingLocation):
    return {
        "id": str(location.id),
        "technician_id": str(location.technician_id),
        "booking_id": str(location.booking_id) if location.booking_id else None,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "accuracy": location.accuracy,
        "speed": location.speed,
        "heading": location.heading,
        "source": location.source,
        "recorded_at": location.recorded_at.isoformat(),
    }


async def _get_technician_by_user(db: AsyncSession, user_id: str):
    return (
        await db.execute(select(Technician).where(Technician.user_id == UUID(user_id), Technician.is_active == True))
    ).scalar_one_or_none()


async def _get_technician_or_404(db: AsyncSession, technician_id: UUID):
    technician = (
        await db.execute(select(Technician).where(Technician.id == technician_id, Technician.is_active == True))
    ).scalar_one_or_none()
    if not technician:
        raise HTTPException(status_code=404, detail="Technician not found")
    return technician


async def _get_booking_or_404(db: AsyncSession, booking_id: UUID):
    booking = (await db.execute(select(Booking).where(Booking.id == booking_id, Booking.is_active == True))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


async def _get_latest_location(
    db: AsyncSession,
    technician_id: UUID,
    booking_id: UUID | None = None,
):
    query = select(TrackingLocation).where(
        TrackingLocation.technician_id == technician_id,
        TrackingLocation.is_active == True,
    )
    if booking_id:
        query = query.where(TrackingLocation.booking_id == booking_id)
    return (
        await db.execute(query.order_by(TrackingLocation.recorded_at.desc(), TrackingLocation.created_at.desc()).limit(1))
    ).scalars().first()


async def _ensure_tracking_access(db: AsyncSession, booking: Booking, current_user: dict):
    role = current_user["role"]
    if role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT", "INVENTORY_MANAGER"}:
        return
    if role == "TECHNICIAN":
        technician = await _get_technician_by_user(db, current_user["user_id"])
        if not technician or booking.technician_id != technician.id:
            raise HTTPException(status_code=403, detail="Access denied")
        return
    if role == "CUSTOMER":
        customer = (
            await db.execute(select(Customer).where(Customer.user_id == UUID(current_user["user_id"]), Customer.is_active == True))
        ).scalar_one_or_none()
        if not customer or booking.customer_id != customer.id:
            raise HTTPException(status_code=403, detail="Access denied")
        return
    raise HTTPException(status_code=403, detail="Access denied")


@router.post("/update-location", summary="Update GPS location")
async def update_location(
    payload: UpdateLocationRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    if current_user["role"] == "TECHNICIAN":
        technician = await _get_technician_by_user(db, current_user["user_id"])
        if not technician:
            raise HTTPException(status_code=404, detail="Technician profile not found")
    else:
        if not payload.technician_id:
            raise HTTPException(status_code=400, detail="technician_id is required for admin/CCO location updates")
        technician = await _get_technician_or_404(db, UUID(payload.technician_id))

    booking_id = UUID(payload.booking_id) if payload.booking_id else None
    if booking_id:
        booking = await _get_booking_or_404(db, booking_id)
        if booking.technician_id and booking.technician_id != technician.id:
            raise HTTPException(status_code=400, detail="Booking is assigned to a different technician")
    else:
        booking = (
            await db.execute(
                select(Booking)
                .where(
                    Booking.technician_id == technician.id,
                    Booking.status.in_(
                        [
                            BookingStatus.ASSIGNED,
                            BookingStatus.ACCEPTED,
                            BookingStatus.EN_ROUTE,
                            BookingStatus.ARRIVED,
                            BookingStatus.INSPECTING,
                            BookingStatus.IN_PROGRESS,
                        ]
                    ),
                    Booking.is_active == True,
                )
                .order_by(Booking.created_at.desc())
            )
        ).scalars().first()
        booking_id = booking.id if booking else None

    location = TrackingLocation(
        technician_id=technician.id,
        booking_id=booking_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy=payload.accuracy,
        speed=payload.speed,
        heading=payload.heading,
        source=payload.source,
        recorded_at=payload.recorded_at or datetime.utcnow(),
    )
    db.add(location)
    await db.flush()
    await db.commit()
    return success_response(data=_serialize_location(location), message="Location updated successfully")


@router.get("/technician/{technician_id}", summary="Get technician current location")
async def technician_current_location(
    technician_id: UUID,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    technician = await _get_technician_or_404(db, technician_id)
    location = await _get_latest_location(db, technician.id)
    return success_response(
        data={
            "technician": {
                "id": str(technician.id),
                "name": technician.name,
                "mobile": technician.mobile,
                "status": technician.status.value,
            },
            "current_location": _serialize_location(location) if location else None,
        }
    )


@router.get("/booking/{booking_id}", summary="Get live booking tracking")
async def booking_live_tracking(
    booking_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking_or_404(db, booking_id)
    await _ensure_tracking_access(db, booking, current_user)
    address = (
        await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id, CustomerAddress.is_active == True))
    ).scalar_one_or_none()
    location = None
    technician = None
    if booking.technician_id:
        technician = await _get_technician_or_404(db, booking.technician_id)
        location = await _get_latest_location(db, technician.id, booking.id)
        if not location:
            location = await _get_latest_location(db, technician.id)
    return success_response(
        data={
            "booking_id": str(booking.id),
            "booking_number": booking.booking_number,
            "status": booking.status.value,
            "technician": (
                {
                    "id": str(technician.id),
                    "name": technician.name,
                    "mobile": technician.mobile,
                }
                if technician
                else None
            ),
            "destination": (
                {
                    "address_id": str(address.id),
                    "city": address.city,
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                }
                if address
                else None
            ),
            "current_location": _serialize_location(location) if location else None,
        }
    )


@router.get("/history/{technician_id}", summary="Get tracking history")
async def tracking_history(
    technician_id: UUID,
    booking_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    technician = await _get_technician_or_404(db, technician_id)
    if current_user["role"] == "TECHNICIAN":
        own_technician = await _get_technician_by_user(db, current_user["user_id"])
        if not own_technician or own_technician.id != technician.id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif current_user["role"] == "CUSTOMER":
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(TrackingLocation).where(
        TrackingLocation.technician_id == technician.id,
        TrackingLocation.is_active == True,
    )
    if booking_id:
        query = query.where(TrackingLocation.booking_id == booking_id)
    locations = (
        await db.execute(query.order_by(TrackingLocation.recorded_at.desc(), TrackingLocation.created_at.desc()).limit(limit))
    ).scalars().all()
    return success_response(
        data={
            "technician": {"id": str(technician.id), "name": technician.name},
            "items": [_serialize_location(location) for location in locations],
        }
    )
