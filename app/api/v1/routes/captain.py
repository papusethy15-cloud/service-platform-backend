import json
"""
Captain App — /api/v1/captain

Endpoints specifically for the technician mobile app (Captain App).
Auth is shared with the main /auth routes — technicians log in via OTP
with the same send-otp / verify-otp endpoints, which return a JWT with
role=TECHNICIAN. These endpoints require that JWT.

GET  /captain/me                — fetch own technician profile
PUT  /captain/me/status         — go online / offline
PUT  /captain/me/fcm-token      — register / update FCM push token
PUT  /captain/me/location       — update last known GPS position
GET  /captain/me/jobs           — today's assigned jobs
GET  /captain/me/earnings       — wallet balance + today's earnings
GET  /captain/me/wallet/transactions — paginated wallet transaction history
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime, timezone, timedelta

from app.core.database import get_db
from app.api.deps import get_current_user, AdminOrTech, TechnicianOnly
from app.models.technician import Technician
from app.models.user import User
from app.models.booking import Booking, BookingStatus
from app.models.tracking import TrackingLocation
import traceback
from app.utils.response import success_response

router = APIRouter()


# ── helpers ───────────────────────────────────────────────────────────────────

async def _get_technician_for_user(user_id: str, db: AsyncSession) -> Technician:
    """Resolve the authenticated user_id → Technician row. 403 if not found."""
    result = await db.execute(
        select(Technician).where(Technician.user_id == user_id)
    )
    tech = result.scalar_one_or_none()
    if not tech:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No technician profile linked to this account."
        )
    return tech


def _tech_profile(tech: Technician) -> dict:
    return {
        "id":               str(tech.id),
        "user_id":          str(tech.user_id),
        "name":             tech.name,
        "mobile":           tech.mobile,
        "email":            tech.email,
        "technician_code":  tech.technician_code,
        "city":             tech.city,
        "area":             tech.area,
        "rating":           tech.rating,
        "total_jobs":       tech.total_jobs,
        "status":           tech.status.value if tech.status else None,
        "is_online":        tech.is_online,
        "profile_image":    tech.profile_image,
        "experience_years": tech.experience_years,
        "last_lat":         tech.last_lat,
        "last_lng":         tech.last_lng,
        "last_seen_at":     tech.last_seen_at.isoformat() if tech.last_seen_at else None,
    }


# ── schemas ───────────────────────────────────────────────────────────────────

class StatusUpdate(BaseModel):
    is_online: bool

class FCMTokenUpdate(BaseModel):
    fcm_token: str

class LocationUpdate(BaseModel):
    lat: float
    lng: float


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/me", summary="Captain: my profile [Technician]")
async def captain_me(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    tech = await _get_technician_for_user(current_user["user_id"], db)
    return success_response(data=_tech_profile(tech))


@router.put("/me/status", summary="Captain: go online / offline [Technician]")
async def update_status(
    payload: StatusUpdate,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    tech = await _get_technician_for_user(current_user["user_id"], db)
    was_offline = not tech.is_online
    tech.is_online = payload.is_online
    await db.commit()

    # When a technician comes online, try to assign any pending auto-assign bookings
    if payload.is_online and was_offline:
        from app.api.v1.routes.bookings import _sweep_pending_auto_assign
        from app.core.background_tasks import track_task
        track_task(_sweep_pending_auto_assign(current_user["user_id"]))

    return success_response(
        data={"is_online": tech.is_online},
        message="Status updated"
    )


@router.put("/me/fcm-token", summary="Captain: register FCM push token [Technician]")
async def update_fcm_token(
    payload: FCMTokenUpdate,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    tech = await _get_technician_for_user(current_user["user_id"], db)
    tech.fcm_token = payload.fcm_token
    await db.commit()
    return success_response(message="FCM token registered")


@router.put("/me/location", summary="Captain: update GPS position [Technician]")
async def update_location(
    payload: LocationUpdate,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    tech = await _get_technician_for_user(current_user["user_id"], db)
    tech.last_lat     = payload.lat
    tech.last_lng     = payload.lng
    tech.last_seen_at = datetime.now(timezone.utc)

    # ── Also record this ping in tracking_locations so the customer's
    # live tracking screen (GET /tracking/booking/{id}) has something to
    # read. Previously this endpoint only touched the technician row,
    # which the tracking table never sees — the customer map stayed
    # empty even while the technician app was successfully sending pings.
    active_booking = (
        await db.execute(
            select(Booking)
            .where(
                Booking.technician_id == tech.id,
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

    location = TrackingLocation(
        technician_id=tech.id,
        booking_id=active_booking.id if active_booking else None,
        latitude=payload.lat,
        longitude=payload.lng,
        source="captain_app",
        recorded_at=datetime.utcnow(),  # naive UTC — column is TIMESTAMP WITHOUT TIME ZONE
    )
    db.add(location)
    await db.commit()

    if active_booking:
        try:
            from app.websocket.manager import publish_event, WSEvent, booking_room
            from app.core.background_tasks import track_task
            track_task(publish_event(
                booking_room(str(active_booking.id)),
                WSEvent.TECHNICIAN_LOCATION_UPDATE,
                {
                    "technician_id": str(tech.id),
                    "booking_id": str(active_booking.id),
                    "latitude": payload.lat,
                    "longitude": payload.lng,
                    "recorded_at": location.recorded_at.isoformat(),
                },
            ))
        except Exception:
            pass

    return success_response(
        data={"lat": tech.last_lat, "lng": tech.last_lng},
        message="Location updated"
    )


@router.post("/me/ping", summary="Captain: heartbeat ping to keep online status alive [Technician]")
async def captain_ping(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the app every 5 minutes when technician is online.
    Updates last_seen_at. Background task uses this to auto-offline
    technicians who have been unreachable for > 10 minutes.
    """
    tech = await _get_technician_for_user(current_user["user_id"], db)
    tech.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    return success_response(data={"last_seen_at": tech.last_seen_at.isoformat()}, message="Ping received")


@router.post("/me/restore-online", summary="Captain: restore online status on app reopen [Technician]")
async def restore_online(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Called when technician reopens the app. If last_seen_at was within 10 minutes,
    the backend has kept them online — confirm current is_online status to app.
    If last_seen_at > 10 minutes ago, they were auto-offlined — return is_online=false.
    """
    tech = await _get_technician_for_user(current_user["user_id"], db)
    now = datetime.now(timezone.utc)

    if tech.last_seen_at:
        elapsed = (now - tech.last_seen_at).total_seconds()
        if elapsed > 600 and tech.is_online:  # 10 minutes
            tech.is_online = False
            tech.last_seen_at = None
            await db.commit()

    return success_response(
        data={"is_online": tech.is_online, "last_seen_at": tech.last_seen_at.isoformat() if tech.last_seen_at else None},
        message="Online status restored"
    )


@router.get("/me/jobs", summary="Captain: assigned jobs [Technician]")
async def captain_my_jobs(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns bookings assigned to this technician, ordered by assignment
    accepted_at descending (newest assignment first).

    Includes:
    - All active statuses (ASSIGNED, ACCEPTED, EN_ROUTE, ARRIVED,
      INSPECTING, IN_PROGRESS, WORK_STARTED)
    - Recently completed/closed jobs from the last 30 days so the
      technician can see their full recent history.
    - CANCELLED jobs are excluded.

    Joins AssignmentHistory to get the real accepted_at / assignment_id
    so sorting reflects when the technician was actually assigned.
    """
    from app.models.booking import Booking
    from app.models.assignment import AssignmentHistory
    from app.models.customer import Customer, CustomerAddress
    tech = await _get_technician_for_user(current_user["user_id"], db)

    try:
        # 30-day lookback window for completed/closed jobs
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        # Fetch the most-recent AssignmentHistory row per booking for this tech
        # using a subquery alias so we can order by it.
        latest_assign = (
            select(
                AssignmentHistory.booking_id,
                func.max(AssignmentHistory.created_at).label("assigned_at"),
            )
            .where(AssignmentHistory.technician_id == tech.id)
            .group_by(AssignmentHistory.booking_id)
            .subquery("latest_assign")
        )

        result = await db.execute(
            select(
                Booking,
                Customer,
                CustomerAddress,
                AssignmentHistory,
                latest_assign.c.assigned_at,
            )
            .outerjoin(Customer, Booking.customer_id == Customer.id)
            .outerjoin(CustomerAddress, Booking.address_id == CustomerAddress.id)
            .outerjoin(
                latest_assign,
                latest_assign.c.booking_id == Booking.id,
            )
            .outerjoin(
                AssignmentHistory,
                (AssignmentHistory.booking_id == Booking.id)
                & (AssignmentHistory.technician_id == tech.id)
                & (AssignmentHistory.created_at == latest_assign.c.assigned_at),
            )
            .where(
                Booking.technician_id == tech.id,
                # Exclude ASSIGNED (pending acceptance — shown in incoming screen, not jobs list)
                # Exclude CANCELLED
                Booking.status.notin_(["CANCELLED", "ASSIGNED"]),
                # Active jobs: always included
                # Completed/closed/paid: only within 30-day window.
                # Use created_at as fallback if updated_at column is missing on VPS.
                (
                    Booking.status.notin_(["COMPLETED", "PAID", "CLOSED", "SETTLED"])
                    | (Booking.created_at >= cutoff)
                ),
            )
            .order_by(latest_assign.c.assigned_at.desc().nullslast(), Booking.created_at.desc())
            .limit(100)
        )
        rows = result.all()

        jobs = []
        for booking, customer, addr, assignment, assigned_at in rows:
            if addr:
                address_parts = [
                    addr.address_line1 or "",
                    addr.address_line2 or "",
                    addr.city or "",
                    addr.state or "",
                    addr.pincode or "",
                ]
                resolved_city = addr.city
            else:
                address_parts = [
                    booking.address_line or "",
                    booking.city or "",
                    booking.pincode or "",
                ]
                resolved_city = booking.city

            full_address = ", ".join(p for p in address_parts if p) or "Address not provided"
            booking_status = booking.status.value if hasattr(booking.status, "value") else str(booking.status)

            jobs.append({
                "assignment_id":  str(assignment.id) if assignment else None,
                "booking_id":     str(booking.id),
                "booking_number": booking.booking_number,
                "status":         booking_status,
                "customer_name":  customer.name if customer else "Customer",
                "address":        full_address,
                "city":           resolved_city,
                "scheduled_date": str(booking.scheduled_date) if booking.scheduled_date else None,
                "scheduled_time": booking.scheduled_slot or None,
                "service_name":   booking.service_name or None,
                "customer_phone": customer.mobile if customer else None,
                "latitude":       float(addr.latitude) if addr and addr.latitude else None,
                "longitude":      float(addr.longitude) if addr and addr.longitude else None,
                # Sorting metadata — used by the app to keep newest-assigned first
                "assigned_at":    assigned_at.isoformat() if assigned_at else booking.created_at.isoformat(),
                # Inspection data — so captain app knows if CCO already submitted
                "inspection_notes":         booking.inspection_notes,
                "inspection_photos":        (json.loads(booking.inspection_photos) if booking.inspection_photos else []),
                "inspection_submitted_by":  booking.inspection_submitted_by,
                # Repair stage before RESCHEDULED — captain app uses this to resume
                # at the correct step (e.g. IN_PROGRESS) instead of treating the
                # rescheduled booking as a brand-new visit.
                "pre_reschedule_status":    booking.pre_reschedule_status,
            })

        return success_response(data={"jobs": jobs, "total": len(jobs)})

    except Exception as exc:
        logger.error(
            "[captain/me/jobs] Unhandled error: %s\n%s",
            exc, traceback.format_exc()
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to load jobs due to a server error. "
                "This is likely a database schema issue — a required column may be "
                f"missing from the VPS database. Error: {type(exc).__name__}: {exc}"
            ),
        )


@router.get("/me/jobs/pending", summary="Captain: pending job requests awaiting accept/reject [Technician]")
async def captain_pending_assignments(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns assignments in ASSIGNED status (not yet accepted/rejected) for
    this technician, with the response_deadline used to drive the
    Captain App's accept/reject countdown screen.
    """
    from app.models.assignment import AssignmentHistory, AssignmentStatus
    from app.models.booking import Booking, BookingStatus
    from app.models.customer import Customer

    tech = await _get_technician_for_user(current_user["user_id"], db)

    # ── Self-heal stale assignments ─────────────────────────────────────────
    # The normal expiry path is an in-process asyncio task (_timeout_watcher
    # in assignments.py) started when the assignment is created. If the
    # backend process restarts before that task fires (deploy, crash, dev
    # reload), the in-memory timer is lost and the row is stuck in ASSIGNED
    # forever — even though its response_deadline has long passed (or, for
    # older rows, was never set at all). Every time the technician opens the
    # app, this stale row was being returned and briefly flashing the
    # accept/reject screen before it self-expired. Sweep them here so a
    # restart can never leave a permanently-stuck pending request.
    stale_result = await db.execute(
        select(AssignmentHistory).where(
            AssignmentHistory.technician_id == tech.id,
            AssignmentHistory.status == AssignmentStatus.ASSIGNED,
            (AssignmentHistory.response_deadline.is_(None))
            | (AssignmentHistory.response_deadline < datetime.now(timezone.utc)),
        )
    )
    stale_assignments = stale_result.scalars().all()
    for stale in stale_assignments:
        stale.status = AssignmentStatus.TIMEOUT
        stale_booking = (await db.execute(
            select(Booking).where(Booking.id == stale.booking_id)
        )).scalar_one_or_none()
        if stale_booking:
            if stale_booking.technician_id == tech.id:
                stale_booking.technician_id = None
            if stale_booking.status in (BookingStatus.ASSIGNED, BookingStatus.ACCEPTED):
                stale_booking.status = BookingStatus.CONFIRMED
    if stale_assignments:
        await db.commit()

    from app.models.customer import CustomerAddress as CA
    result = await db.execute(
        select(AssignmentHistory, Booking, Customer, CA)
        .join(Booking, AssignmentHistory.booking_id == Booking.id)
        .outerjoin(Customer, Booking.customer_id == Customer.id)
        .outerjoin(CA, Booking.address_id == CA.id)
        .where(
            AssignmentHistory.technician_id == tech.id,
            AssignmentHistory.status == AssignmentStatus.ASSIGNED,
        )
        .order_by(AssignmentHistory.created_at.desc())
    )
    rows = result.all()

    requests = []
    for assignment, booking, customer, addr in rows:
        # Resolve address: prefer CustomerAddress FK (admin bookings), fall back to free-text
        if addr:
            address_parts = [
                addr.address_line1 or "",
                addr.address_line2 or "",
                addr.city or "",
                addr.state or "",
                addr.pincode or "",
            ]
        else:
            address_parts = [
                booking.address_line or "",
                booking.city or "",
                booking.pincode or "",
            ]
        full_address = ", ".join(p for p in address_parts if p) or "Address not provided"
        requests.append({
            "assignment_id":     str(assignment.id),
            "booking_id":        str(booking.id),
            "booking_number":    booking.booking_number,
            "customer_name":     customer.name if customer else "Customer",
            "address":           full_address,
            "city":              addr.city if addr else booking.city,
            "latitude":          addr.latitude if addr else None,
            "longitude":         addr.longitude if addr else None,
            "scheduled_date":    str(booking.scheduled_date) if booking.scheduled_date else None,
            "scheduled_time":    booking.scheduled_slot or None,
            "service_name":      booking.service_name or None,
            "score":             assignment.score,
            "response_deadline": assignment.response_deadline.isoformat() if assignment.response_deadline else None,
        })

    return success_response(data={"requests": requests, "total": len(requests)})


@router.get("/me/earnings", summary="Captain: wallet balance + today's earnings [Technician]")
async def captain_earnings(
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet, WalletTransaction
    tech = await _get_technician_for_user(current_user["user_id"], db)

    wallet_result = await db.execute(
        select(Wallet).where(Wallet.technician_id == tech.id)
    )
    wallet = wallet_result.scalar_one_or_none()

    # IST midnight = UTC 18:30 previous day; compute correctly
    _IST = timezone(timedelta(hours=5, minutes=30))
    _ist_now = datetime.now(timezone.utc).astimezone(_IST)
    today_start = _ist_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    today_credit = 0.0
    if wallet:
        credit_result = await db.execute(
            select(func.sum(WalletTransaction.amount))
            .where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.transaction_type == "CREDIT",
                WalletTransaction.created_at >= today_start,
            )
        )
        today_credit = float(credit_result.scalar() or 0)

    # Commission amounts on hold (PENDING + APPROVED but not yet PAID → not in wallet yet)
    from app.models.commission import Commission
    comm_result = await db.execute(
        select(
            func.coalesce(func.sum(Commission.commission_amount).filter(Commission.status == "PENDING"),  0).label("pending"),
            func.coalesce(func.sum(Commission.commission_amount).filter(Commission.status == "APPROVED"), 0).label("approved"),
        ).where(Commission.technician_id == tech.id)
    )
    comm_row = comm_result.one()
    pending_commission  = float(comm_row.pending  or 0)
    approved_commission = float(comm_row.approved or 0)
    commission_on_hold  = round(pending_commission + approved_commission, 2)

    # ── Pending withdrawal requests (submitted but not yet approved/rejected) ──
    # These amounts are still IN wallet.balance but logically already "spoken for".
    from app.models.wallet import WithdrawalRequest
    pending_wr_result = await db.execute(
        select(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0))
        .where(
            WithdrawalRequest.technician_id == tech.id,
            WithdrawalRequest.status == "PENDING",
        )
    )
    pending_withdrawal_amount = float(pending_wr_result.scalar() or 0)

    raw_balance = float(wallet.balance) if wallet else 0.0
    # Withdrawable = actual balance minus any in-flight pending withdrawal amounts
    withdrawable_balance = max(round(raw_balance - pending_withdrawal_amount, 2), 0.0)

    has_pending_withdrawal = pending_withdrawal_amount > 0

    return success_response(data={
        "balance":                  raw_balance,
        "withdrawable_balance":     withdrawable_balance,        # what technician can actually request
        "pending_withdrawal_amount": round(pending_withdrawal_amount, 2),  # already-submitted, not yet processed
        "has_pending_withdrawal":   has_pending_withdrawal,
        "today_earnings":           today_credit,
        "total_jobs":               tech.total_jobs,
        "rating":                   tech.rating,
        "commission_on_hold":       commission_on_hold,   # settled but not yet paid into wallet
        "pending_commission":       round(pending_commission,  2),
        "approved_commission":      round(approved_commission, 2),
    })


@router.get("/me/wallet/transactions", summary="Captain: wallet transaction history [Technician]")
async def captain_wallet_transactions(
    page: int = 1,
    page_size: int = 20,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet, WalletTransaction
    tech = await _get_technician_for_user(current_user["user_id"], db)

    wallet_result = await db.execute(
        select(Wallet).where(Wallet.technician_id == tech.id)
    )
    wallet = wallet_result.scalar_one_or_none()

    if not wallet:
        return success_response(data={"items": [], "total": 0, "page": page, "page_size": page_size})

    count_result = await db.execute(
        select(func.count(WalletTransaction.id)).where(WalletTransaction.wallet_id == wallet.id)
    )
    total = count_result.scalar() or 0

    rows_result = await db.execute(
        select(WalletTransaction)
        .where(WalletTransaction.wallet_id == wallet.id)
        .order_by(WalletTransaction.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()

    items = [{
        "id":                str(r.id),
        "transaction_type":  r.transaction_type,
        "amount":            float(r.amount),
        "balance_before":    float(r.balance_before) if r.balance_before is not None else None,
        "balance_after":     float(r.balance_after) if r.balance_after is not None else None,
        "reference_id":      r.reference_id,
        "description":       r.description,
        "status":            r.status,
        "created_at":        r.created_at.isoformat() if r.created_at else None,
    } for r in rows]

    return success_response(data={
        "items": items, "total": total, "page": page, "page_size": page_size,
        "wallet_balance":    float(wallet.balance),
        "total_earned":      float(wallet.total_earned or 0),
        "total_withdrawn":   float(wallet.total_withdrawn or 0),
    })


# ── Commission for a booking ───────────────────────────────────────────────────
@router.get("/me/bookings/{booking_id}/commission", summary="Captain: get commission for a booking [Technician]")
async def captain_booking_commission(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import Commission
    from app.models.booking import Booking
    tech = await _get_technician_for_user(current_user["user_id"], db)

    # Verify booking belongs to this technician
    booking = (await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.technician_id == tech.id)
    )).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    rows = (await db.execute(
        select(Commission).where(Commission.booking_id == booking_id, Commission.technician_id == tech.id)
    )).scalars().all()

    items = [{
        "id": str(c.id),
        "item_type": c.item_type,
        "item_name": c.item_name,
        "base_amount": round(c.base_amount or 0, 2),
        "commission_amount": round(c.commission_amount or 0, 2),
        "status": c.status,
        "payout_date": c.payout_date.isoformat() if c.payout_date else None,
        "notes": c.notes,
    } for c in rows]

    total_commission = round(sum(c.commission_amount or 0 for c in rows), 2)
    paid_commission  = round(sum(c.commission_amount or 0 for c in rows if c.status == "PAID"), 2)
    is_settled       = bool(rows) and all(c.status == "PAID" for c in rows)

    return success_response(data={
        "booking_id": str(booking_id),
        "items": items,
        "total_commission": total_commission,
        "paid_commission": paid_commission,
        "is_settled": is_settled,
        "has_commission": bool(rows),
    })


# ── Rate customer after job completion ─────────────────────────────────────────
@router.post("/me/bookings/{booking_id}/rate-customer", summary="Captain: rate customer [Technician]")
async def captain_rate_customer(
    booking_id: UUID,
    payload: dict,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking
    from app.models.customer import Customer
    tech = await _get_technician_for_user(current_user["user_id"], db)

    booking = (await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.technician_id == tech.id)
    )).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    rating = float(payload.get("rating", 0))
    if not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")

    # Store in booking's customer_rating field; create if model supports it
    # We store on the booking record itself
    if hasattr(booking, "technician_to_customer_rating"):
        booking.technician_to_customer_rating = rating
    if hasattr(booking, "technician_to_customer_notes"):
        booking.technician_to_customer_notes = payload.get("notes", "")
    await db.commit()

    return success_response(data={
        "booking_id": str(booking_id),
        "rating": rating,
        "notes": payload.get("notes", ""),
    }, message="Customer rated successfully")


# ── Get customer rating given by technician for a booking ──────────────────────
@router.get("/me/bookings/{booking_id}/customer-rating", summary="Captain: get customer rating [Technician]")
async def captain_get_customer_rating(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking
    tech = await _get_technician_for_user(current_user["user_id"], db)

    booking = (await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.technician_id == tech.id)
    )).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    rating = getattr(booking, "technician_to_customer_rating", None)
    notes  = getattr(booking, "technician_to_customer_notes",  None)

    return success_response(data={
        "booking_id": str(booking_id),
        "rating": rating,
        "notes": notes,
        "has_rated": rating is not None,
    })


# ── Get customer review of this booking ────────────────────────────────────────
@router.get("/me/bookings/{booking_id}/customer-review", summary="Captain: get customer review for this booking [Technician]")
async def captain_get_customer_review(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.booking import Booking
    from app.models.technician import TechnicianRating
    from app.models.customer import Customer
    tech = await _get_technician_for_user(current_user["user_id"], db)

    booking = (await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.technician_id == tech.id)
    )).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Get customer's rating of this technician for this booking
    rating_row = (await db.execute(
        select(TechnicianRating).where(
            TechnicianRating.technician_id == tech.id,
            TechnicianRating.booking_id == booking_id,
        )
    )).scalar_one_or_none()

    if not rating_row:
        return success_response(data={"booking_id": str(booking_id), "has_review": False})

    # Get customer name
    customer_name = None
    if rating_row.customer_id:
        cust = (await db.execute(select(Customer).where(Customer.id == rating_row.customer_id))).scalar_one_or_none()
        customer_name = cust.name if cust else None

    return success_response(data={
        "booking_id": str(booking_id),
        "has_review": True,
        "rating": rating_row.rating,
        "review": rating_row.review,
        "customer_name": customer_name,
        "created_at": rating_row.created_at.isoformat() if hasattr(rating_row, "created_at") and rating_row.created_at else None,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Cash Collections — Technician view
# GET /captain/me/cash-collections
# Returns all CashCollectionRecord rows for the logged-in technician with
# totals so the app can show what the technician needs to deposit to the office.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/me/cash-collections", summary="Captain: my cash collection records [Technician]")
async def captain_cash_collections(
    status: Optional[str] = Query(None, description="PENDING | COLLECTED"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the technician's own cash-collection records.
    - PENDING  = cash collected from customer, not yet handed to office
    - COLLECTED = admin/CCO has already received this cash
    Also returns summary totals so the app can display the 'amount to deposit'.
    """
    from app.models.payment import CashCollectionRecord, CashCollectionStatus
    from app.models.booking import Booking
    from app.models.invoice import Invoice
    from app.models.customer import Customer

    tech = await _get_technician_for_user(current_user["user_id"], db)

    q = (
        select(CashCollectionRecord, Customer, Booking, Invoice)
        .outerjoin(Customer, Customer.id == CashCollectionRecord.customer_id)
        .outerjoin(Booking,  Booking.id  == CashCollectionRecord.booking_id)
        .outerjoin(Invoice,  Invoice.id  == CashCollectionRecord.invoice_id)
        .where(
            CashCollectionRecord.technician_id == tech.id,
            CashCollectionRecord.is_active == True,
        )
    )

    if status:
        try:
            q = q.where(CashCollectionRecord.status == CashCollectionStatus(status))
        except ValueError:
            pass

    # Totals (all time, regardless of pagination)
    all_rows = (await db.execute(
        select(CashCollectionRecord)
        .where(CashCollectionRecord.technician_id == tech.id, CashCollectionRecord.is_active == True)
    )).scalars().all()
    total_pending   = sum(r.amount for r in all_rows if r.status == CashCollectionStatus.PENDING)
    total_collected = sum(r.amount for r in all_rows if r.status == CashCollectionStatus.COLLECTED)

    total_count = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar_one()

    rows = (await db.execute(
        q.order_by(CashCollectionRecord.created_at.desc())
         .offset((page - 1) * per_page).limit(per_page)
    )).all()

    items = []
    for rec, cust, bk, inv in rows:
        items.append({
            "id":               str(rec.id),
            "booking_id":       str(rec.booking_id),
            "booking_number":   bk.booking_number  if bk  else None,
            "invoice_id":       str(rec.invoice_id),
            "invoice_number":   inv.invoice_number if inv else None,
            "customer_name":    cust.name          if cust else None,
            "customer_mobile":  cust.mobile        if cust else None,
            "amount":           rec.amount,
            "status":           rec.status.value,
            "collected_at":     rec.collected_at.isoformat() if rec.collected_at else None,
            "notes":            rec.notes,
            "created_at":       rec.created_at.isoformat() if rec.created_at else None,
        })

    return success_response(data={
        "summary": {
            "total_pending":   total_pending,
            "total_collected": total_collected,
            "pending_count":   sum(1 for r in all_rows if r.status == CashCollectionStatus.PENDING),
            "collected_count": sum(1 for r in all_rows if r.status == CashCollectionStatus.COLLECTED),
        },
        "items":    items,
        "total":    total_count,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total_count + per_page - 1) // per_page),
    })


# ─────────────────────────────────────────────────────────────────────────────
# My Commissions — full list across all bookings
# GET /captain/me/commissions
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/me/commissions", summary="Captain: all my commissions across bookings [Technician]")
async def captain_my_commissions(
    status: Optional[str] = Query(None, description="PENDING | APPROVED | PAID"),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all Commission rows for the logged-in technician, grouped context:
    - Each item includes booking_number so technician knows which job earned what.
    - Summary totals: pending / approved / paid amounts.
    - item_type: SERVICE or PART so tech knows what the commission is for.

    Status lifecycle:
      PENDING  → settled by admin, waiting for approval
      APPROVED → admin approved, will be paid soon
      PAID     → credited to wallet
    """
    from app.models.commission import Commission
    from app.models.booking import Booking

    tech = await _get_technician_for_user(current_user["user_id"], db)

    q = (
        select(Commission, Booking)
        .outerjoin(Booking, Booking.id == Commission.booking_id)
        .where(Commission.technician_id == tech.id)
    )
    if status:
        q = q.where(Commission.status == status)

    # Totals across ALL records (ignore pagination)
    all_rows = (await db.execute(
        select(Commission).where(Commission.technician_id == tech.id)
    )).scalars().all()
    total_pending  = round(sum(r.commission_amount or 0 for r in all_rows if r.status == "PENDING"),  2)
    total_approved = round(sum(r.commission_amount or 0 for r in all_rows if r.status == "APPROVED"), 2)
    total_paid     = round(sum(r.commission_amount or 0 for r in all_rows if r.status == "PAID"),     2)
    count_pending  = sum(1 for r in all_rows if r.status == "PENDING")
    count_approved = sum(1 for r in all_rows if r.status == "APPROVED")
    count_paid     = sum(1 for r in all_rows if r.status == "PAID")

    total_count = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar_one()

    rows = (await db.execute(
        q.order_by(Commission.created_at.desc())
         .offset((page - 1) * per_page).limit(per_page)
    )).all()

    items = []
    for comm, booking in rows:
        items.append({
            "id":                str(comm.id),
            "booking_id":        str(comm.booking_id) if comm.booking_id else None,
            "booking_number":    booking.booking_number if booking else None,
            "item_type":         comm.item_type,       # SERVICE | PART
            "item_name":         comm.item_name,
            "item_quantity":     comm.item_quantity,
            "part_source":       comm.part_source,     # OFFICE_STOCK | MARKET_PURCHASE | None
            "base_amount":       round(comm.base_amount or 0, 2),
            "commission_amount": round(comm.commission_amount or 0, 2),
            "status":            comm.status,           # PENDING | APPROVED | PAID
            "payout_date":       comm.payout_date.isoformat() if comm.payout_date else None,
            "notes":             comm.notes,
            "created_at":        comm.created_at.isoformat() if comm.created_at else None,
        })

    return success_response(data={
        "summary": {
            "total_pending":   total_pending,
            "total_approved":  total_approved,
            "total_paid":      total_paid,
            "count_pending":   count_pending,
            "count_approved":  count_approved,
            "count_paid":      count_paid,
            "total_lifetime":  round(total_pending + total_approved + total_paid, 2),
        },
        "items":    items,
        "total":    total_count,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total_count + per_page - 1) // per_page),
    })


# ══════════════════════════════════════════════════════════════════════
# WITHDRAWAL REQUESTS — Captain side
# ══════════════════════════════════════════════════════════════════════

class WithdrawalRequestCreate(BaseModel):
    amount: float
    upi_id: Optional[str] = None
    bank_account: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_name: Optional[str] = None
    notes: Optional[str] = None


@router.post("/me/withdrawal-requests", summary="Captain: request a withdrawal [Technician]")
async def captain_request_withdrawal(
    payload: WithdrawalRequestCreate,
    current_user: dict = Depends(TechnicianOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet, WithdrawalRequest

    tech = await _get_technician_for_user(current_user["user_id"], db)
    wallet = (await db.execute(select(Wallet).where(Wallet.technician_id == tech.id))).scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=400, detail="No wallet found. You have no balance to withdraw.")

    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")

    MIN_WITHDRAWAL = 100.0
    if payload.amount < MIN_WITHDRAWAL:
        raise HTTPException(status_code=400, detail=f"Minimum withdrawal amount is ₹{MIN_WITHDRAWAL:.0f}.")

    # ── Validate payment method first ──
    if not payload.upi_id and not payload.bank_account:
        raise HTTPException(status_code=400, detail="Provide either a UPI ID or bank account details.")

    # ── Check for any existing PENDING withdrawal request (one at a time only) ──
    existing_pending = (await db.execute(
        select(WithdrawalRequest).where(
            WithdrawalRequest.technician_id == tech.id,
            WithdrawalRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if existing_pending:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a pending withdrawal request of ₹{existing_pending.amount:.0f}. Wait for admin to process it before submitting a new one."
        )

    # ── Compute how much is truly withdrawable ──
    # wallet.balance includes amounts from PENDING withdrawal requests (not yet debited),
    # so we subtract any in-flight pending total to get the true available amount.
    pending_wr_total_result = await db.execute(
        select(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0))
        .where(
            WithdrawalRequest.technician_id == tech.id,
            WithdrawalRequest.status == "PENDING",
        )
    )
    pending_wr_total = float(pending_wr_total_result.scalar() or 0)
    raw_balance = float(wallet.balance or 0)
    withdrawable = max(round(raw_balance - pending_wr_total, 2), 0.0)

    if payload.amount > raw_balance:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Wallet balance: ₹{raw_balance:.2f}. Requested: ₹{payload.amount:.2f}."
        )

    if payload.amount > withdrawable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient withdrawable balance. "
                f"Wallet: ₹{raw_balance:.2f}, "
                f"Pending withdrawal hold: ₹{pending_wr_total:.2f}, "
                f"Available to withdraw: ₹{withdrawable:.2f}."
            )
        )

    # ── Amount precision guard ──
    if round(payload.amount, 2) != payload.amount:
        payload.amount = round(payload.amount, 2)

    req = WithdrawalRequest(
        technician_id=tech.id,
        wallet_id=wallet.id,
        amount=round(payload.amount, 2),
        status="PENDING",
        upi_id=payload.upi_id,
        bank_account=payload.bank_account,
        bank_ifsc=payload.bank_ifsc,
        bank_name=payload.bank_name,
        notes=payload.notes,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    return success_response(data={
        "id": str(req.id),
        "amount": req.amount,
        "status": req.status,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }, message="Withdrawal request submitted. Admin will process it shortly.")


@router.get("/me/withdrawal-requests", summary="Captain: list my withdrawal requests [Technician]")
async def captain_list_withdrawal_requests(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
    current_user: dict = Depends(TechnicianOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import WithdrawalRequest

    tech = await _get_technician_for_user(current_user["user_id"], db)

    total = (await db.execute(
        select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.technician_id == tech.id)
    )).scalar_one()

    rows = (await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.technician_id == tech.id)
        .order_by(WithdrawalRequest.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()

    items = [{
        "id": str(r.id),
        "amount": r.amount,
        "status": r.status,
        "upi_id": r.upi_id,
        "bank_account": r.bank_account,
        "bank_name": r.bank_name,
        "notes": r.notes,
        "admin_notes": r.admin_notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    } for r in rows]

    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })
