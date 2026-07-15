import asyncio
from app.core.background_tasks import track_task
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
import random, string
from app.websocket.manager import publish_event, WSEvent, booking_room, ADMIN_BOOKINGS_ROOM

from app.core.database import get_db
from app.models.booking import Booking, BookingStatus, BookingStatusLog, BookingSource
from app.models.customer import Customer, CustomerAddress
from app.models.technician import Technician
from app.models.tracking import TrackingLocation
from app.models.domain import Domain, DomainCity
from app.models.city import City as CityModel
from app.models.quotation import Quotation as QuotationModel, QuotationServiceItem, QuotationPartItem, QuotationAppliance
from app.api.v1.schemas.booking import (
    CreateBookingRequest, UpdateBookingRequest,
    RescheduleBookingRequest, AssignTechnicianRequest,
    CancelBookingRequest, SubmitInspectionRequest, VisitingChargeRequest
)
from app.api.deps import AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response, iso
from app.utils.notify import push_to_technician
from pydantic import BaseModel
from typing import Optional
_BM = BaseModel   # alias used by legacy classes in this file
_Opt = Optional  # alias used by legacy classes in this file

router = APIRouter()


def generate_booking_number():
    suffix = ''.join(random.choices(string.digits, k=8))
    return f"BK{suffix}"

async def _add_status_log(db, booking_id, status, user_id=None, notes=None):
    log = BookingStatusLog(
        booking_id=booking_id,
        status=status,
        changed_by=UUID(user_id) if user_id else None,
        notes=notes
    )
    db.add(log)

async def _get_booking_or_404(db: AsyncSession, booking_id: UUID) -> Booking:
    result = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking

async def _cancel_booking(db: AsyncSession, booking: Booking, user_id: str, reason: str):
    """Authoritative, immediate cancellation — only for admin/CCO callers."""
    NON_CANCELLABLE = [BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.CLOSED, BookingStatus.SETTLED]
    if booking.status in NON_CANCELLABLE:
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking in {booking.status.value} state")
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_reason = reason
    booking.pre_cancel_status = None
    await _add_status_log(db, booking.id, BookingStatus.CANCELLED, user_id, reason)
    track_task(publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "CANCELLED"}))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "CANCELLED"}))
    # Push to customer (admin-side cancellation)
    if booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            track_task(_ptc(db=db, customer_id=booking.customer_id,
                title="Booking Cancelled ❌",
                body=f"Booking {booking.booking_number} has been cancelled. {reason or ''}".strip(),
                notif_type="BOOKING",
                data={"type": "BOOKING_CANCELLED", "booking_id": str(booking.id), "booking_number": booking.booking_number}))
        except Exception: pass


# Statuses reached before a technician has arrived on-site. Cancellation requests
# raised while a booking is in one of these states go through admin/CCO
# verification (CANCELLATION_REQUESTED) rather than cancelling immediately.
PRE_ARRIVAL_STATUSES = [
    BookingStatus.PENDING,
    BookingStatus.CONFIRMED,
    BookingStatus.ASSIGNED,
    BookingStatus.ACCEPTED,
    BookingStatus.TECHNICIAN_ACCEPTED,
    BookingStatus.EN_ROUTE,
]


async def _request_cancellation(db: AsyncSession, booking: Booking, user_id: str, reason: str):
    """
    Customer/technician-initiated cancellation before the technician has
    arrived. Does NOT cancel immediately — parks the booking in
    CANCELLATION_REQUESTED for admin/CCO to confirm or reject, per the
    'all cancellations get verified' rule.
    """
    if booking.status not in PRE_ARRIVAL_STATUSES:
        if booking.status in (BookingStatus.ARRIVED, BookingStatus.INSPECTING, BookingStatus.IN_PROGRESS):
            raise HTTPException(
                status_code=400,
                detail="Technician has already arrived at the address — use the visiting-charge flow instead of a direct cancellation.",
            )
        raise HTTPException(status_code=400, detail=f"Cannot request cancellation while booking is in {booking.status.value} state")

    booking.pre_cancel_status = booking.status.value
    booking.status = BookingStatus.CANCELLATION_REQUESTED
    booking.cancelled_reason = reason
    await _add_status_log(
        db, booking.id, BookingStatus.CANCELLATION_REQUESTED, user_id,
        f"Cancellation requested — awaiting admin/CCO confirmation. Reason: {reason}",
    )
    track_task(publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "CANCELLATION_REQUESTED"}))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "CANCELLATION_REQUESTED",
         "message": f"Booking {booking.booking_number} — cancellation requested, needs admin/CCO confirmation."}))

    # Best-effort FCM ping to admin/CCO, mirroring the manual-assign-needed pattern.
    try:
        from app.models.user import User
        from app.utils.fcm import send_simple_push
        admin_users = (await db.execute(
            select(User).where(
                User.role.in_(["SUPER_ADMIN", "ADMIN", "CCO"]),
                User.fcm_token.isnot(None),
                User.is_active == True,
            )
        )).scalars().all()
        for admin_user in admin_users:
            track_task(send_simple_push(
                fcm_token=admin_user.fcm_token,
                title="Cancellation Needs Confirmation",
                body=f"Booking {booking.booking_number} — cancellation requested. Please confirm or reject.",
                data={"type": "BOOKING_CANCELLATION_REQUESTED", "booking_id": str(booking.id), "booking_number": booking.booking_number},
            ))
    except Exception:
        pass

# ── AUTO-ASSIGN HELPER ────────────────────────────────────────
async def _maybe_auto_assign(booking_id_str: str, booking_number: str, triggered_by_user_id: str) -> None:
    """
    Background task: check auto_assign_enabled setting and, if ON, assign
    the booking to the best available ONLINE technician.

    IMPORTANT: Opens its OWN database session — the request session is
    already closed by the time this coroutine runs via ensure_future.

    Checks performed:
      1. auto_assign_enabled == 'true'
      2. booking has service_id (needed for skill scoring)
      3. At least one ACTIVE + ONLINE technician exists
      4. Picks best via score (skill, rating, workload, GPS proximity)
      5. Applies assignment → WS events + FCM push + starts timeout watcher
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    from app.core.database import AsyncSessionLocal
    from app.models.system_setting import SystemSetting
    from app.models.assignment import AssignmentHistory, AssignmentStatus, AssignmentType
    from app.models.technician import Technician as _Tech, TechnicianStatus
    from app.api.v1.routes.assignments import (
        _get_default_rules, _apply_assignment, _start_watcher,
    )
    from uuid import UUID as _UUID

    async with AsyncSessionLocal() as db:
        try:
            # 1. Read auto_assign_enabled setting
            setting_row = (await db.execute(
                select(SystemSetting).where(
                    SystemSetting.group == "dispatch",
                    SystemSetting.key == "auto_assign_enabled",
                )
            )).scalar_one_or_none()
            enabled = (setting_row.value if setting_row else "true").strip().lower()
            if enabled != "true":
                _logger.info(f"Auto-assign OFF — skipping booking {booking_number}")
                return

            # 2. Reload booking from fresh session
            booking = (await db.execute(
                select(Booking).where(Booking.id == _UUID(booking_id_str))
            )).scalar_one_or_none()
            if not booking:
                _logger.warning(f"Auto-assign: booking {booking_number} not found")
                return

            # 3. Need service_id for skill matching
            if not booking.service_id:
                _logger.info(f"Auto-assign skipped — booking {booking_number} has no service_id")
                return

            # 4. Must have at least one online technician
            online_count = (await db.execute(
                select(func.count(_Tech.id)).where(
                    _Tech.status == TechnicianStatus.ACTIVE,
                    _Tech.is_online == True,
                )
            )).scalar_one()
            if online_count == 0:
                _logger.info(f"Auto-assign: no online technicians — flagging booking {booking_number} for deferred assignment")
                # Mark the booking (PENDING or CONFIRMED) so it will be auto-assigned when a technician comes online
                if booking and not booking.technician_id and booking.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
                    existing_notes = booking.notes or ""
                    if "[PENDING_AUTO_ASSIGN]" not in existing_notes:
                        booking.notes = (existing_notes + "\n[PENDING_AUTO_ASSIGN]").strip()
                    await db.commit()
                return

            # 5. Get rules + pick best online technician
            rules = await _get_default_rules(db)
            score, technician, workload = await _pick_best_technician_online(db, booking, rules)

            _logger.info(f"Auto-assign: booking {booking_number} → technician {technician.name} (score={score:.1f})")

            # 6. Apply assignment — returns the AssignmentHistory row; commits + fires WS + FCM
            new_asgn = await _apply_assignment(
                db, booking, technician, AssignmentType.AUTO,
                triggered_by_user_id,
                "Auto-assigned on booking creation",
                score,
                rules.response_timeout_minutes,
            )

            # 7. Start two-phase timeout watcher
            if new_asgn:
                _start_watcher(new_asgn)

        except Exception as _e:
            _logger.warning(f"Auto-assign failed for booking {booking_number}: {_e}", exc_info=True)


async def _pick_best_technician_online(db: AsyncSession, booking: "Booking", rules):
    """
    Variant of _pick_best_technician that restricts candidates to is_online=True technicians.
    Falls back to all online technicians when skill filter would leave zero candidates.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    from app.models.technician import Technician as _Tech, TechnicianSkill, TechnicianStatus
    from app.models.customer import CustomerAddress
    from app.api.v1.routes.assignments import _haversine_km, _get_active_workload, ACTIVE_BOOKING_STATUSES
    from fastapi import HTTPException
    import math

    technicians = (
        await db.execute(
            select(_Tech).where(
                _Tech.status == TechnicianStatus.ACTIVE,
                _Tech.is_online == True,
            )
        )
    ).scalars().all()
    if not technicians:
        raise HTTPException(status_code=404, detail="No online technicians")

    skill_rows = (
        await db.execute(select(TechnicianSkill).where(TechnicianSkill.service_id == booking.service_id))
    ).scalars().all()
    skill_match_ids = {row.technician_id for row in skill_rows}

    if rules.require_skill_match and skill_match_ids:
        # Only enforce skill filter when skills are actually registered for this service.
        # If no skills are mapped to the service, fall back to all online technicians
        # (avoids silent failure when skills table is empty during initial setup).
        skilled = [t for t in technicians if t.id in skill_match_ids]
        if skilled:
            technicians = skilled
        else:
            _logger.warning(
                f"Auto-assign: require_skill_match=True but no technician has skill for "
                f"service {booking.service_id} — falling back to all {len(technicians)} online technician(s)"
            )

    address = None
    _booking_lat, _booking_lng = None, None
    if booking.address_id:
        address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id))).scalar_one_or_none()
        if address and getattr(address, 'latitude', None) and getattr(address, 'longitude', None):
            _booking_lat, _booking_lng = address.latitude, address.longitude

    if rules.prefer_same_city and address:
        same_city = [t for t in technicians if t.city and t.city.lower() == address.city.lower()]
        if same_city:
            technicians = same_city

    scored = []
    for tech in technicians:
        workload = await _get_active_workload(db, tech.id)
        if workload >= rules.max_active_bookings:
            continue
        score = 0.0
        if tech.id in skill_match_ids:
            score += 50
        if rules.prefer_high_rating:
            score += tech.rating * 20
        if rules.prefer_low_workload:
            score += max(0, 30 - workload * 10)
        score += max(0, 20 - (tech.total_jobs or 0) * 0.1)
        if _booking_lat and _booking_lng and tech.last_lat and tech.last_lng:
            dist_km = _haversine_km(tech.last_lat, tech.last_lng, _booking_lat, _booking_lng)
            score += max(0, 30 - dist_km)
        scored.append((score, tech, workload))

    if not scored:
        raise HTTPException(status_code=404, detail="No available online technician")

    scored.sort(key=lambda item: (item[0], item[1].rating, -item[2]), reverse=True)
    return scored[0]


async def _sweep_pending_auto_assign(triggered_by_user_id: str) -> None:
    """
    Called when a technician comes online.
    Finds all PENDING unassigned bookings flagged with [PENDING_AUTO_ASSIGN]
    and attempts to assign the newly-online technician (or best available).
    Runs in its own DB session.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    from app.core.database import AsyncSessionLocal
    from app.models.system_setting import SystemSetting
    from app.models.assignment import AssignmentType
    from app.models.technician import Technician as _Tech, TechnicianStatus
    from app.api.v1.routes.assignments import (
        _get_default_rules, _apply_assignment, _start_watcher,
    )

    async with AsyncSessionLocal() as db:
        try:
            # Check auto-assign is still enabled
            setting_row = (await db.execute(
                select(SystemSetting).where(
                    SystemSetting.group == "dispatch",
                    SystemSetting.key == "auto_assign_enabled",
                )
            )).scalar_one_or_none()
            enabled = (setting_row.value if setting_row else "true").strip().lower()
            if enabled != "true":
                return

            # Find all unassigned PENDING/CONFIRMED bookings.
            # Includes both:
            #   a) Bookings flagged with [PENDING_AUTO_ASSIGN] (created after this fix)
            #   b) Any PENDING/CONFIRMED booking with no technician (safety net for older bookings)
            # (website bookings start as PENDING; CCO/Admin bookings start as CONFIRMED)
            pending_bookings = (await db.execute(
                select(Booking).where(
                    Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
                    Booking.technician_id == None,
                )
            )).scalars().all()

            if not pending_bookings:
                return

            _logger.info(f"Auto-assign sweep: {len(pending_bookings)} pending booking(s) to process")
            rules = await _get_default_rules(db)

            for booking in pending_bookings:
                try:
                    score, technician, workload = await _pick_best_technician_online(db, booking, rules)
                    _logger.info(f"Deferred auto-assign: booking {booking.booking_number} → {technician.name}")

                    # Clear the flag from notes before commit inside _apply_assignment
                    if booking.notes:
                        booking.notes = booking.notes.replace("[PENDING_AUTO_ASSIGN]", "").strip() or None

                    # _apply_assignment commits + fires WS + FCM; returns AssignmentHistory
                    new_asgn = await _apply_assignment(
                        db, booking, technician, AssignmentType.AUTO,
                        triggered_by_user_id,
                        "Auto-assigned on technician coming online",
                        score,
                        rules.response_timeout_minutes,
                    )

                    # Start two-phase timeout watcher
                    if new_asgn:
                        _start_watcher(new_asgn)

                except Exception as _e:
                    _logger.warning(f"Deferred auto-assign failed for booking {booking.booking_number}: {_e}")

        except Exception as _e:
            _logger.warning(f"Auto-assign sweep error: {_e}", exc_info=True)


# ── CREATE BOOKING ─────────────────────────────────────────────
@router.post("", summary="Create a new booking [Customer/CCO/Admin]")
async def create_booking(
    payload: CreateBookingRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    # Resolve customer — admin can pass customer_id explicitly
    if payload.customer_id and current_user["role"] in ("SUPER_ADMIN", "ADMIN", "CCO"):
        customer = (await db.execute(select(Customer).where(Customer.id == UUID(payload.customer_id)))).scalar_one_or_none()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
    else:
        if current_user["role"] in ("SUPER_ADMIN", "ADMIN", "CCO"):
            # Admin must always supply customer_id explicitly
            raise HTTPException(status_code=400, detail="customer_id is required for admin/CCO booking creation")
        result = await db.execute(select(Customer).where(Customer.user_id == UUID(current_user["user_id"])))
        customer = result.scalar_one_or_none()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer profile not found")

    # ── Profile completeness gate (customers only) ─────────────────────────────
    # Block booking if the customer's name is missing or is a system placeholder
    # (e.g. "New Customer", "New User") or their mobile number is missing.
    # Admins/CCO creating bookings on behalf of customers are exempt.
    _PLACEHOLDER_NAMES = {"new customer", "new user", "customer", "user"}
    if customer and current_user["role"] == "CUSTOMER":
        _cust_name = (customer.name or "").strip()
        _cust_mobile = (customer.mobile or "").strip()
        if not _cust_name or _cust_name.lower() in _PLACEHOLDER_NAMES:
            raise HTTPException(
                status_code=422,
                detail="INCOMPLETE_PROFILE:name:Please update your name before booking."
            )
        if not _cust_mobile:
            raise HTTPException(
                status_code=422,
                detail="INCOMPLETE_PROFILE:mobile:Please add your mobile number before booking."
            )

    # ── Duplicate booking check ────────────────────────────────────────────────
    # Rule: same customer + same SERVICE CATEGORY + same address + active booking → block.
    #
    # Rationale:
    #   • "Same service + same address" is too strict — customers may legitimately book
    #     a "Washing Machine Deep Clean" and a "Washing Machine Repair" at the same address.
    #   • "Same category + same address" is the right boundary: e.g. if you already have
    #     an active "AC Services" booking at your home address, you can't book another
    #     "AC Services" job there until the first one completes/is cancelled.
    #   • Different category (e.g. AC Services vs Washing Machine) at the same address
    #     is always allowed — these are independent jobs.
    #   • Re-booking is always allowed once a booking reaches a terminal status:
    #     COMPLETED, CANCELLED, PAID, CLOSED, SETTLED, REFUND_INITIATED.
    if customer and not payload.force_duplicate and payload.service_id and payload.address_id:
        from app.models.service import Service as _Service
        # Terminal statuses — job is done or abandoned; customer may re-book freely.
        REBOOKABLE_STATUSES = [
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.PAID,
            BookingStatus.CLOSED,
            BookingStatus.SETTLED,
            BookingStatus.REFUND_INITIATED,
        ]
        # Resolve the category_id of the service being booked now.
        _incoming_svc = (await db.execute(
            select(_Service).where(_Service.id == UUID(payload.service_id))
        )).scalar_one_or_none()
        _incoming_category_id = _incoming_svc.category_id if _incoming_svc else None

        if _incoming_category_id:
            # Find any active booking for this customer at this address in the SAME category.
            # We JOIN Booking → Service to get the category of each existing booking's service.
            dup_q = (
                select(Booking)
                .join(_Service, _Service.id == Booking.service_id)
                .where(
                    Booking.customer_id == customer.id,
                    Booking.address_id == UUID(payload.address_id),
                    _Service.category_id == _incoming_category_id,
                    Booking.status.notin_(REBOOKABLE_STATUSES),
                )
            )
            duplicate = (await db.execute(dup_q)).scalar_one_or_none()
            if duplicate:
                from app.models.service import ServiceCategory as _Cat
                _cat = (await db.execute(
                    select(_Cat).where(_Cat.id == _incoming_category_id)
                )).scalar_one_or_none()
                _cat_name = _cat.name if _cat else "this category"
                raise HTTPException(
                    status_code=409,
                    detail=f"DUPLICATE:{duplicate.booking_number}:{duplicate.status.value}:{_cat_name}"
                )

    # Fetch service for pricing/name (service_id is optional for chatbot/web bookings)
    # Guard: treat empty string the same as None (mobile app may send "" when
    # DomainService.serviceId was not populated from the API response).
    from app.models.service import Service
    if payload.service_id is not None and payload.service_id.strip() == "":
        payload.service_id = None
    service = None
    if payload.service_id:
        service = (await db.execute(select(Service).where(Service.id == UUID(payload.service_id)))).scalar_one_or_none()

    # Normalise to midnight naive datetime (date-only semantics, IST-safe)
    from datetime import timezone as _tz, datetime as _dt_cb
    sched_date = payload.scheduled_date
    if sched_date:
        # Strip tz first, then take just the date part stored as midnight
        _naive = sched_date.replace(tzinfo=None) if sched_date.tzinfo is not None else sched_date
        sched_date = _dt_cb(_naive.year, _naive.month, _naive.day, 0, 0, 0)

    # ── Pricing + coupon resolution ───────────────────────────────────────────
    # base_price: use payload value if provided, otherwise derive from service
    base_price = payload.base_amount if payload.base_amount else (service.base_price if service else 0.0)

    # coupon_discount: from payload (customer already validated via /coupons/validate)
    coupon_discount = float(payload.coupon_discount or 0.0)
    coupon_id = UUID(payload.coupon_id) if payload.coupon_id else None
    coupon_code = payload.coupon_code or None

    # Clamp discount so it never exceeds base_price
    coupon_discount = min(coupon_discount, base_price)

    # final total after coupon
    final_total = max(base_price - coupon_discount, 0.0)

    # Increment coupon used_count when a coupon is applied
    if coupon_id:
        from app.models.coupon import Coupon
        _cpn = (await db.execute(select(Coupon).where(Coupon.id == coupon_id))).scalar_one_or_none()
        if _cpn:
            _cpn.used_count = (_cpn.used_count or 0) + 1

    # ── Resolve city_id + city name ──────────────────────────────────────
    from app.models.city import City as CityModel
    from app.models.domain import DomainCity
    resolved_city_id: UUID | None = None
    resolved_city_name: str | None = payload.city  # start with whatever was sent
    if payload.city_id:
        resolved_city_id = UUID(payload.city_id)
        # Fetch city name from DB so it's always stored (admin sends city_id but no city text)
        if not resolved_city_name:
            _c = (await db.execute(select(CityModel).where(CityModel.id == resolved_city_id))).scalar_one_or_none()
            if _c:
                resolved_city_name = _c.name
    elif payload.city and payload.domain_id:
        # Try to find a city linked to this domain that matches the city name
        dc_row = (await db.execute(
            select(DomainCity, CityModel)
            .join(CityModel, CityModel.id == DomainCity.city_id)
            .where(
                DomainCity.domain_id == UUID(payload.domain_id),
                DomainCity.is_active == True,
                CityModel.is_active == True,
                CityModel.name.ilike(payload.city),
            )
        )).first()
        if dc_row:
            resolved_city_id = dc_row.CityModel.id
            resolved_city_name = dc_row.CityModel.name
    elif payload.city:
        # Fallback: match city by name globally
        city_row = (await db.execute(
            select(CityModel).where(CityModel.name.ilike(payload.city), CityModel.is_active == True)
        )).scalar_one_or_none()
        if city_row:
            resolved_city_id = city_row.id
            resolved_city_name = city_row.name

    # ── If city still unresolved, derive from the customer address ───────────
    # Mobile app sends address_id but no city text/city_id — look up via address.city
    if not resolved_city_id and payload.address_id:
        _addr_for_city = (await db.execute(
            select(CustomerAddress).where(CustomerAddress.id == UUID(payload.address_id))
        )).scalar_one_or_none()
        if _addr_for_city and _addr_for_city.city:
            _addr_city_row = (await db.execute(
                select(CityModel).where(
                    CityModel.name.ilike(_addr_for_city.city),
                    CityModel.is_active == True,
                )
            )).scalar_one_or_none()
            if _addr_city_row:
                resolved_city_id = _addr_city_row.id
                resolved_city_name = _addr_city_row.name
            else:
                resolved_city_name = _addr_for_city.city  # store free-text city from address

    booking = Booking(
        booking_number=generate_booking_number(),
        customer_id=customer.id if customer else UUID(current_user["user_id"]),
        service_id=UUID(payload.service_id) if payload.service_id else None,
        service_name=service.name if service else payload.service_name,
        address_id=UUID(payload.address_id) if payload.address_id else None,
        address_line=payload.address_line,
        city=resolved_city_name,
        city_id=resolved_city_id,
        scheduled_date=sched_date,
        scheduled_slot=payload.scheduled_slot,
        notes=payload.notes,
        appliance_brand=payload.appliance_brand,
        appliance_model=payload.appliance_model,
        appliance_id=UUID(payload.appliance_id) if getattr(payload, 'appliance_id', None) else None,
        source=BookingSource(payload.source),
        status=BookingStatus.CONFIRMED,
        priority=payload.priority or "NORMAL",
        domain_id=UUID(payload.domain_id) if payload.domain_id else None,
        base_amount=base_price,
        discount_amount=coupon_discount,
        gst_amount=round((service.gst_percent / 100.0) * final_total) if service and service.gst_percent else 0,
        total_amount=final_total,
        coupon_id=coupon_id,
        coupon_code=coupon_code,
        coupon_discount=coupon_discount,
    )
    db.add(booking)
    await db.flush()
    await _add_status_log(db, booking.id, BookingStatus.CONFIRMED, current_user["user_id"], "Booking created by admin/CCO")
    await db.commit()
    await db.refresh(booking)

    # ── Auto-assign: fire-and-forget in background with its own DB session ──
    track_task(_maybe_auto_assign(str(booking.id), booking.booking_number, current_user["user_id"]))

    # Push booking confirmation to customer
    if customer and customer.id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            _sched = str(sched_date) if sched_date else "soon"
            track_task(_ptc(db=db, customer_id=customer.id,
                title="Booking Confirmed 🎉",
                body=f"Your booking {booking.booking_number} for {booking.service_name} is confirmed for {_sched}.",
                notif_type="BOOKING",
                data={"type": "BOOKING_CONFIRMED", "booking_id": str(booking.id), "booking_number": booking.booking_number}))
        except Exception: pass

    return success_response(data={"id": str(booking.id), "booking_number": booking.booking_number, "status": booking.status.value}, message="Booking created")

# ── SLOT SUMMARY ───────────────────────────────────────────────
@router.get("/slot-summary", summary="Slot booking counts for a date [Admin/CCO]")
async def slot_summary(
    date: str = Query(..., description="YYYY-MM-DD"),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns per-slot booking counts for the given date.
    Only counts ACTIVE bookings (excludes CANCELLED, COMPLETED, PAID, CLOSED, SETTLED).
    Used by the reschedule modal to show real-time slot availability.
    """
    from sqlalchemy import cast as _cast, Date as _SADate, func as _func
    from datetime import datetime as _dt_ss

    try:
        target_date = _dt_ss.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD.")

    # Active statuses — slot is considered occupied
    active_statuses = [
        BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.ASSIGNED,
        BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.ARRIVED,
        BookingStatus.INSPECTING, BookingStatus.IN_PROGRESS, BookingStatus.WORK_STARTED,
        BookingStatus.WORK_PAUSED, BookingStatus.QUOTATION_APPROVED,
        BookingStatus.TECHNICIAN_ACCEPTED, BookingStatus.PENDING_VERIFICATION,
        BookingStatus.RESCHEDULED, BookingStatus.INVOICE_GENERATED,
        BookingStatus.PAYMENT_PENDING, BookingStatus.CANCELLATION_REQUESTED,
    ]

    rows = (await db.execute(
        select(Booking.scheduled_slot, _func.count(Booking.id))
        .where(
            _cast(Booking.scheduled_date, _SADate) == target_date,
            Booking.status.in_(active_statuses),
            Booking.scheduled_slot.isnot(None),
        )
        .group_by(Booking.scheduled_slot)
    )).all()

    counts = {row[0]: row[1] for row in rows if row[0]}
    return success_response(data={"date": date, "slot_counts": counts})


# ── LIST BOOKINGS ──────────────────────────────────────────────
@router.get("", summary="List bookings [Admin/CCO or own]")
async def list_bookings(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = Query(None),
    exclude_status: str = Query(None),
    priority: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    customer_id: str = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.service import Service
    from sqlalchemy import or_, and_, cast, String as SAString
    from datetime import datetime

    # Build base query with LEFT JOINs for customer, service, technician, domain, city
    q = (
        select(Booking, Customer, Service, Technician, Domain, CityModel)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .outerjoin(Domain, Domain.id == Booking.domain_id)
        .outerjoin(CityModel, CityModel.id == Booking.city_id)
    )

    # Role-based scoping
    if current_user["role"] == "CUSTOMER":
        cust = (await db.execute(select(Customer).where(Customer.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
        if cust:
            q = q.where(Booking.customer_id == cust.id)
        else:
            q = q.where(Booking.id == None)
    elif current_user["role"] == "TECHNICIAN":
        tech = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
        if tech:
            q = q.where(Booking.technician_id == tech.id)
        else:
            q = q.where(Booking.id == None)

    # CCO-specific: filter by specific customer_id (used in customer detail view)
    if customer_id and current_user["role"] in ("CCO", "ADMIN", "SUPER_ADMIN"):
        q = q.where(Booking.customer_id == UUID(customer_id))

    # Filters — status supports single value OR comma-separated list
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            q = q.where(Booking.status == BookingStatus(statuses[0]))
        elif len(statuses) > 1:
            q = q.where(Booking.status.in_([BookingStatus(s) for s in statuses]))
    # exclude_status: hide completed/closed/paid records from default CCO list view
    if exclude_status:
        ex_statuses = [s.strip() for s in exclude_status.split(",") if s.strip()]
        try:
            q = q.where(Booking.status.notin_([BookingStatus(s) for s in ex_statuses]))
        except Exception:
            pass
    if priority:
        q = q.where(Booking.priority == priority)
    if search:
        s = f"%{search.strip()}%"
        q = q.where(or_(
            Booking.booking_number.ilike(s),
            Customer.name.ilike(s),
            Customer.mobile.ilike(s),
            Booking.service_name.ilike(s),
            Service.name.ilike(s),
            Technician.name.ilike(s),
        ))
    if date_from:
        # Cast the stored DateTime to DATE for exact-day comparison regardless of
        # time component (scheduled_date is stored as midnight naive datetime).
        from sqlalchemy import cast as sa_cast, Date as SADate
        q = q.where(sa_cast(Booking.scheduled_date, SADate) >= datetime.fromisoformat(date_from).date())
    if date_to:
        from sqlalchemy import cast as sa_cast, Date as SADate
        q = q.where(sa_cast(Booking.scheduled_date, SADate) <= datetime.fromisoformat(date_to).date())

    # Count using the same fully-scoped query (role filters + status filters applied).
    # Wrap q as a subquery and count from it so role-based WHERE clauses
    # (e.g. customer_id = <this customer>) are preserved in the count.
    try:
        count_subq = q.subquery()
        count_q = select(func.count()).select_from(count_subq)
        total = (await db.execute(count_q)).scalar_one()
    except Exception:
        # Fallback: fetch all IDs and count in Python (never loses scope)
        id_rows = (await db.execute(q.with_only_columns(Booking.id))).all()
        total = len(id_rows)

    rows = (await db.execute(
        q.order_by(Booking.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    )).all()

    items = []
    for idx, (b, cust, svc, tech, domain, city_row) in enumerate(rows):
        svc_name = b.service_name or (svc.name if svc else None) or "—"
        items.append({
            "id": str(b.id),
            "booking_number": b.booking_number,
            "status": b.status.value,
            "priority": b.priority or "NORMAL",
            "source": b.source.value if b.source else "—",
            "scheduled_date": b.scheduled_date.strftime("%Y-%m-%d") if b.scheduled_date else None,
            "scheduled_slot": b.scheduled_slot or "—",
            "total_amount": b.total_amount or 0,
            "base_amount": b.base_amount or 0,
            "gst_amount": b.gst_amount or 0,
            "service_name": svc_name,
            "customer_name": cust.name if cust else "—",
            "customer_mobile": cust.mobile if cust else "—",
            "customer_code": cust.customer_code if cust else "—",
            "technician_name": tech.name if tech else None,
            "technician_mobile": tech.mobile if tech else None,
            "technician_confirmed": b.status.value in ("ACCEPTED", "EN_ROUTE", "ARRIVED", "INSPECTING", "IN_PROGRESS", "COMPLETED", "INVOICE_GENERATED", "PAYMENT_PENDING", "PAID", "CLOSED", "SETTLED"),  # False when ASSIGNED = dispatched but not yet accepted
            "appliance_brand": b.appliance_brand or None,
            "appliance_model": b.appliance_model or None,
            "notes": b.notes or None,
            "cancelled_reason": b.cancelled_reason or None,
            "city": b.city or (city_row.name if city_row else None),
            "city_id": str(b.city_id) if b.city_id else None,
            "coupon_code": b.coupon_code,
            "coupon_discount": b.coupon_discount or 0.0,
            "domain_name": domain.name if domain else None,
            "created_at": iso(b.created_at) if b.created_at else None,
        })

    # ── Enrich with quotation summary (batch, one query) ──────────────
    if items:
        booking_ids = [UUID(item["id"]) for item in items]
        # Get best quotation per booking: prefer APPROVED > SUBMITTED > DRAFT > others
        # Fetch all active quotations for these bookings
        q_rows = (await db.execute(
            select(
                QuotationModel.booking_id,
                QuotationModel.id,
                QuotationModel.status,
                QuotationModel.total_amount,
                QuotationModel.services_total,
                QuotationModel.parts_total,
                QuotationModel.quotation_number,
            ).where(
                QuotationModel.booking_id.in_(booking_ids),
                # Include all quotations (active and inactive) for accurate total aggregation
            )
        )).all()

        # Count services + parts per quotation
        q_ids = [r.id for r in q_rows]
        svc_counts: dict = {}
        part_counts: dict = {}
        if q_ids:
            svc_res = (await db.execute(
                select(QuotationServiceItem.quotation_id, func.count(QuotationServiceItem.id))
                .where(QuotationServiceItem.quotation_id.in_(q_ids))
                .group_by(QuotationServiceItem.quotation_id)
            )).all()
            svc_counts = {str(r[0]): r[1] for r in svc_res}
            part_res = (await db.execute(
                select(QuotationPartItem.quotation_id, func.count(QuotationPartItem.id))
                .where(QuotationPartItem.quotation_id.in_(q_ids))
                .group_by(QuotationPartItem.quotation_id)
            )).all()
            part_counts = {str(r[0]): r[1] for r in part_res}

        # Build map: booking_id → AGGREGATED totals across ALL quotations
        # Also track best-status quotation for display label (APPROVED > SUBMITTED > DRAFT …)
        STATUS_RANK = {"APPROVED": 0, "CONVERTED_TO_INVOICE": 0, "SUBMITTED": 1, "DRAFT": 2, "REVISED": 3, "REJECTED": 4, "EXPIRED": 5}
        booking_q_map: dict = {}
        for r in q_rows:
            bid = str(r.booking_id)
            status_val = r.status.value if hasattr(r.status, 'value') else r.status
            rank = STATUS_RANK.get(status_val, 99)
            if bid not in booking_q_map:
                booking_q_map[bid] = {
                    "_best_rank": rank,
                    "quotation_status": status_val,       # status of best quotation
                    "quotation_id": str(r.id),            # id of best quotation
                    "quotation_number": r.quotation_number,
                    "quotation_total": r.total_amount or 0,
                    "quotation_services_total": r.services_total or 0,
                    "quotation_parts_total": r.parts_total or 0,
                    "quotation_service_count": svc_counts.get(str(r.id), 0),
                    "quotation_part_count": part_counts.get(str(r.id), 0),
                }
            else:
                agg = booking_q_map[bid]
                # Sum amounts across all quotations
                agg["quotation_total"] += r.total_amount or 0
                agg["quotation_services_total"] += r.services_total or 0
                agg["quotation_parts_total"] += r.parts_total or 0
                agg["quotation_service_count"] += svc_counts.get(str(r.id), 0)
                agg["quotation_part_count"] += part_counts.get(str(r.id), 0)
                # Keep best-status quotation for label
                if rank < agg["_best_rank"]:
                    agg["_best_rank"] = rank
                    agg["quotation_status"] = status_val
                    agg["quotation_id"] = str(r.id)
                    agg["quotation_number"] = r.quotation_number

        # Count total quotations per booking
        q_count_map: dict = {}
        for r in q_rows:
            bid = str(r.booking_id)
            q_count_map[bid] = q_count_map.get(bid, 0) + 1

        # Attach to items
        for item in items:
            qdata = booking_q_map.get(item["id"])
            if qdata:
                item["has_quotation"] = True
                item["quotation_count"] = q_count_map.get(item["id"], 1)
                item.update({k: v for k, v in qdata.items() if not k.startswith("_")})
            else:
                item["has_quotation"] = False
                item["quotation_count"] = 0

    # ── Enrich with Pay Later pending info (batch, one query) ────────────
    # Identifies bookings that have a PENDING PAY_LATER transaction so the
    # list view can surface an ⏰ Pay Later badge without opening the panel.
    if items:
        from app.models.payment import PaymentTransaction, PaymentMethod, PaymentStatus
        booking_ids_uuid = [UUID(item["id"]) for item in items]
        pl_rows = (await db.execute(
            select(
                PaymentTransaction.booking_id,
                func.min(PaymentTransaction.due_collect_at).label("earliest_due"),
            ).where(
                PaymentTransaction.booking_id.in_(booking_ids_uuid),
                PaymentTransaction.method == PaymentMethod.PAY_LATER,
                PaymentTransaction.status == PaymentStatus.PENDING,
            ).group_by(PaymentTransaction.booking_id)
        )).all()
        pl_map = {str(r.booking_id): r.earliest_due for r in pl_rows}
        for item in items:
            due = pl_map.get(item["id"])
            item["has_pay_later"] = due is not None
            item["pay_later_due"] = iso(due) if due else None
    else:
        for item in items:
            item["has_pay_later"] = False
            item["pay_later_due"] = None

    pages = max(1, -(-total // per_page))  # ceil division
    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })

# ── PUBLIC TRACK BOOKING (no auth — booking number and/or phone lookup) ────
@router.get("/track", summary="Track booking by number or phone [Public, No Auth]")
async def track_booking(
    booking_id: str = Query(None, description="Booking number, e.g. BK12345678"),
    phone: str = Query(None, description="Customer mobile number"),
    db: AsyncSession = Depends(get_db),
):
    if not booking_id and not phone:
        raise HTTPException(status_code=400, detail="Provide booking_id or phone")

    query = select(Booking)
    matched_customer = None

    if booking_id:
        query = query.where(Booking.booking_number == booking_id.strip().upper())
    elif phone:
        try:
            from app.utils.phone import normalize_mobile
            normalized_phone = normalize_mobile(phone)
        except Exception:
            # Public endpoint -- don't 422 on a malformed phone, just fall
            # back to a raw match (won't find a normalized record, but
            # won't crash the lookup either).
            normalized_phone = phone.strip()
        matched_customer = (
            await db.execute(select(Customer).where(Customer.mobile == normalized_phone))
        ).scalar_one_or_none()
        if not matched_customer:
            raise HTTPException(status_code=404, detail="Booking not found")
        query = query.where(Booking.customer_id == matched_customer.id).order_by(Booking.created_at.desc())

    booking = (await db.execute(query.limit(1))).scalars().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # If both booking_id and phone were supplied, cross-check phone matches the booking's customer
    if booking_id and phone:
        owner = (await db.execute(select(Customer).where(Customer.id == booking.customer_id))).scalar_one_or_none()
        if not owner or owner.mobile != phone.strip():
            raise HTTPException(status_code=404, detail="Booking not found")

    service_name = booking.service_name
    if booking.service_id:
        from app.models.service import Service
        svc = (await db.execute(select(Service).where(Service.id == booking.service_id))).scalar_one_or_none()
        if svc:
            service_name = svc.name

    technician_name = None
    technician_phone = None
    if booking.technician_id:
        tech = (await db.execute(select(Technician).where(Technician.id == booking.technician_id))).scalar_one_or_none()
        if tech:
            technician_name = tech.name
            technician_phone = tech.mobile

    logs = (await db.execute(
        select(BookingStatusLog).where(BookingStatusLog.booking_id == booking.id).order_by(BookingStatusLog.created_at)
    )).scalars().all()
    timeline = [{
        "status": l.status.value,
        "timestamp": iso(l.created_at),
        "description": l.notes or l.status.value.replace("_", " ").title(),
    } for l in logs]

    return success_response(data={
        "id": str(booking.id),
        "booking_number": booking.booking_number,
        "status": booking.status.value,
        "service_name": service_name or "Service",
        "scheduled_date": booking.scheduled_date.strftime("%Y-%m-%d") if booking.scheduled_date else None,
        "scheduled_time": booking.scheduled_slot,
        "technician_name": technician_name,
        "technician_phone": technician_phone,
        "timeline": timeline,
    })


# ── GET BOOKING ────────────────────────────────────────────────
@router.get("/{booking_id}", summary="Booking details")
async def get_booking(booking_id: UUID, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.service import Service
    row = (await db.execute(
        select(Booking, Customer, Service, Technician, Domain, CityModel)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .outerjoin(Domain, Domain.id == Booking.domain_id)
        .outerjoin(CityModel, CityModel.id == Booking.city_id)
        .where(Booking.id == booking_id)
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    b, cust, svc, tech, domain, city_row = row
    svc_name = b.service_name or (svc.name if svc else None) or "—"
    # Build address string from CustomerAddress if address_id exists (single query)
    addr_str = None
    addr_label = None
    addr_lat = None
    addr_lng = None
    addr_location_source = None
    if b.address_id:
        addr = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == b.address_id))).scalar_one_or_none()
        if addr:
            parts = [p for p in [addr.address_line1, addr.city, addr.state, addr.pincode] if p]
            addr_str             = ", ".join(parts) if parts else None
            addr_label           = addr.label
            addr_lat             = addr.latitude
            addr_lng             = addr.longitude
            addr_location_source = getattr(addr, 'location_source', None)
    if not addr_str:
        parts = [p for p in [b.address_line, b.city, b.pincode] if p]
        addr_str = ", ".join(parts) if parts else None
    return success_response(data={
        "id": str(b.id), "booking_number": b.booking_number,
        "customer_id": str(b.customer_id),
        "customer_name": cust.name if cust else "—",
        "customer_mobile": cust.mobile if cust else "—",
        "customer_code": cust.customer_code if cust else None,
        "technician_id": str(b.technician_id) if b.technician_id else None,
        "technician_name": tech.name if tech else None,
        "technician_mobile": tech.mobile if tech else None,
        "service_id": str(b.service_id) if b.service_id else None,
        "service_name": svc_name,
        "address_id": str(b.address_id) if b.address_id else None,
        "address_str": addr_str,
        "address_label": addr_label,
        "address_latitude": addr_lat,
        "address_longitude": addr_lng,
        "address_location_source": addr_location_source,
        "status": b.status.value, "source": b.source.value if b.source else None,
        "scheduled_date": b.scheduled_date.strftime("%Y-%m-%d") if b.scheduled_date else None,
        "scheduled_slot": b.scheduled_slot,
        "notes": b.notes,
        "appliance_brand": b.appliance_brand, "appliance_model": b.appliance_model,
        "base_amount": b.base_amount or 0, "discount_amount": b.discount_amount or 0,
        "gst_amount": b.gst_amount or 0, "total_amount": b.total_amount or 0,
        "priority": b.priority or "NORMAL",
        "city": b.city or (city_row.name if city_row else None),
        "cancelled_reason": b.cancelled_reason,
        "domain_name": domain.name if domain else None,
        "domain_id": str(b.domain_id) if b.domain_id else None,
        "city_id": str(b.city_id) if b.city_id else None,
        "created_at": iso(b.created_at) if b.created_at else None,
        "updated_at": iso(b.updated_at) if hasattr(b, "updated_at") and b.updated_at else None,
        "inspection_notes": b.inspection_notes,
        "inspection_photos": (json.loads(b.inspection_photos) if b.inspection_photos else []),
        "inspection_submitted_by": b.inspection_submitted_by,
        "pre_reschedule_status": b.pre_reschedule_status,
        "customer_rating": await _get_booking_customer_rating(db, b.id),
    })

# ── UPDATE BOOKING ─────────────────────────────────────────────
@router.put("/{booking_id}", summary="Update booking [Admin/CCO]")
async def update_booking(booking_id: UUID, payload: UpdateBookingRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    if payload.scheduled_date:
        from datetime import timezone as _tz3
        upd_date = payload.scheduled_date
        if upd_date.tzinfo is not None:
            upd_date = upd_date.replace(tzinfo=None)
        booking.scheduled_date = upd_date
    if payload.scheduled_slot: booking.scheduled_slot = payload.scheduled_slot
    if payload.notes: booking.notes = payload.notes
    if payload.priority: booking.priority = payload.priority
    await db.commit()
    return success_response(message="Booking updated")

# ── RESCHEDULE ─────────────────────────────────────────────────
# -- PATCH BOOKING ADDRESS GEO -------------------------------------------
# CCO pastes a WhatsApp/Google Maps URL inside the Booking Detail panel.
# Patches lat/lng on the booking's linked CustomerAddress so the
# technician EN_ROUTE map shows the correct destination.

class BookingGeoUpdateRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    whatsapp_url: Optional[str] = None
    location_source: str = "whatsapp"

@router.patch("/{booking_id}/address-geo",
              summary="Patch GPS on a booking address [CCO/Admin]")
async def patch_booking_address_geo(
    booking_id: UUID,
    payload: BookingGeoUpdateRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    import re as _re
    def _parse(url):
        for pat in [r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
                    r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
                    r"/@(-?\d+\.\d+),(-?\d+\.\d+)",
                    r"loc:(-?\d+\.\d+),(-?\d+\.\d+)"]:
            m = _re.search(pat, url)
            if m: return float(m.group(1)), float(m.group(2))
        return None, None

    booking = await _get_booking_or_404(db, booking_id)
    if not booking.address_id:
        raise HTTPException(status_code=422, detail="Booking has no linked address.")
    address = (await db.execute(
        select(CustomerAddress).where(CustomerAddress.id == booking.address_id)
    )).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found.")

    lat, lng = payload.latitude, payload.longitude
    source = payload.location_source
    if payload.whatsapp_url:
        lat, lng = _parse(payload.whatsapp_url)
        if lat is None:
            raise HTTPException(status_code=422, detail="Could not extract coordinates from URL.")
        source = "whatsapp"
    if lat is None or lng is None:
        raise HTTPException(status_code=422, detail="lat/lng or whatsapp_url required.")

    address.latitude = lat
    address.longitude = lng
    address.location_source = source
    await db.commit()
    return success_response(data={
        "booking_id": str(booking.id), "address_id": str(address.id),
        "latitude": lat, "longitude": lng, "location_source": source,
    }, message="Location saved successfully")

@router.patch("/{booking_id}/reschedule", summary="Reschedule booking")
async def reschedule_booking(booking_id: UUID, payload: RescheduleBookingRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    from datetime import datetime as _dt2, date as _date2
    resched_date = payload.scheduled_date
    # Normalise: if it's a datetime strip tz; if it's a plain date keep as-is
    if isinstance(resched_date, _dt2):
        resched_date = resched_date.replace(tzinfo=None).date()
    booking.scheduled_date = resched_date
    if payload.scheduled_slot: booking.scheduled_slot = payload.scheduled_slot
    # Save the current repair stage so clients can resume at the correct step
    # after the rescheduled visit. Only overwrite if not already RESCHEDULED
    # (i.e. re-rescheduling should keep the original pre-reschedule stage).
    if booking.status != BookingStatus.RESCHEDULED:
        booking.pre_reschedule_status = booking.status.value
    booking.status = BookingStatus.RESCHEDULED
    await _add_status_log(db, booking.id, BookingStatus.RESCHEDULED, current_user["user_id"], payload.reason)
    await db.commit()
    track_task(publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "RESCHEDULED"}))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": "RESCHEDULED"}))
    return success_response(message="Booking rescheduled")

# ── ASSIGN TECHNICIAN ──────────────────────────────────────────
@router.post("/{booking_id}/assign", summary="Assign technician [Admin/CCO]")
async def assign_technician(booking_id: UUID, payload: AssignTechnicianRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    booking.technician_id = UUID(payload.technician_id)
    booking.status = BookingStatus.ASSIGNED
    # Clear any deferred auto-assign flag since we're manually assigning now
    if booking.notes and "[PENDING_AUTO_ASSIGN]" in booking.notes:
        booking.notes = booking.notes.replace("[PENDING_AUTO_ASSIGN]", "").strip() or None
    await _add_status_log(db, booking.id, BookingStatus.ASSIGNED, current_user["user_id"], payload.notes or "Technician assigned")
    await db.commit()
    return success_response(message="Technician assigned")

# ── CANCEL ─────────────────────────────────────────────────────
@router.post("/{booking_id}/verify", summary="Verify booking [Admin/CCO]")
async def verify_booking(booking_id: UUID, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    booking.status = BookingStatus.CONFIRMED
    await _add_status_log(db, booking.id, BookingStatus.CONFIRMED, current_user["user_id"], "Booking verified")
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Booking verified")

@router.delete("/{booking_id}", summary="Delete booking [documented cancel path]")
async def delete_booking(
    booking_id: UUID,
    reason: str = Query("Cancelled via delete endpoint"),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    booking = await _get_booking_or_404(db, booking_id)
    if current_user["role"] in ("SUPER_ADMIN", "ADMIN", "CCO"):
        await _cancel_booking(db, booking, current_user["user_id"], reason)
        await db.commit()
        return success_response(data={"status": booking.status.value}, message="Booking cancelled")
    await _request_cancellation(db, booking, current_user["user_id"], reason)
    await db.commit()
    return success_response(
        data={"status": booking.status.value},
        message="Cancellation requested — awaiting admin/CCO confirmation",
    )

@router.post("/{booking_id}/cancel", summary="Cancel booking")
async def cancel_booking(booking_id: UUID, payload: CancelBookingRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    """
    Admin/CCO cancellations are authoritative and apply immediately.
    Customer/technician cancellations before technician arrival are parked in
    CANCELLATION_REQUESTED and need admin/CCO confirmation (see
    /confirm-cancellation and /reject-cancellation) — this keeps the
    verification rule consistent with the post-arrival visiting-charge path.
    """
    booking = await _get_booking_or_404(db, booking_id)
    if current_user["role"] in ("SUPER_ADMIN", "ADMIN", "CCO"):
        await _cancel_booking(db, booking, current_user["user_id"], payload.reason)
        await db.commit()
        return success_response(data={"status": booking.status.value}, message="Booking cancelled")

    await _request_cancellation(db, booking, current_user["user_id"], payload.reason)
    await db.commit()
    return success_response(
        data={"status": booking.status.value},
        message="Cancellation requested — awaiting admin/CCO confirmation",
    )


@router.post("/{booking_id}/confirm-cancellation", summary="Confirm a pending cancellation request [Admin/CCO]")
async def confirm_cancellation(
    booking_id: UUID,
    payload: CancelBookingRequest | None = None,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status != BookingStatus.CANCELLATION_REQUESTED:
        raise HTTPException(status_code=400, detail=f"Booking is not awaiting cancellation confirmation (current status: {booking.status.value})")
    reason = (payload.reason if payload and payload.reason else booking.cancelled_reason) or "Cancellation confirmed by admin/CCO"
    await _cancel_booking(db, booking, current_user["user_id"], reason)
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Cancellation confirmed — booking cancelled")


@router.post("/{booking_id}/reject-cancellation", summary="Reject a pending cancellation request, restore booking [Admin/CCO]")
async def reject_cancellation(
    booking_id: UUID,
    payload: CancelBookingRequest | None = None,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status != BookingStatus.CANCELLATION_REQUESTED:
        raise HTTPException(status_code=400, detail=f"Booking is not awaiting cancellation confirmation (current status: {booking.status.value})")
    restore_to = booking.pre_cancel_status or BookingStatus.CONFIRMED.value
    try:
        restored_status = BookingStatus(restore_to)
    except ValueError:
        restored_status = BookingStatus.CONFIRMED
    booking.status = restored_status
    booking.pre_cancel_status = None
    note = (payload.reason if payload and payload.reason else None) or "Cancellation request rejected by admin/CCO — booking restored"
    await _add_status_log(db, booking.id, restored_status, current_user["user_id"], note)
    await db.commit()
    track_task(publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": restored_status.value}))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED,
        {"booking_id": str(booking.id), "booking_number": booking.booking_number, "status": restored_status.value}))
    return success_response(data={"status": booking.status.value}, message="Cancellation request rejected — booking restored")

# ── LIFECYCLE ACTIONS ──────────────────────────────────────────
async def _transition(booking_id: UUID, new_status: BookingStatus, current_user: dict, db: AsyncSession, notes: str = None):
    booking = await _get_booking_or_404(db, booking_id)
    # These transitions (arrived / start-inspection / start-work / complete-work / etc.)
    # describe a technician's on-site progress — they make no sense without one assigned.
    if not booking.technician_id:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot update status to {new_status.value}: no technician assigned to this booking. Assign a technician first."
        )
    booking.status = new_status
    # Clear the rescheduled repair-stage tracker once the booking advances to
    # any real on-site status — it has served its purpose and is no longer needed.
    if new_status not in (BookingStatus.RESCHEDULED,) and booking.pre_reschedule_status:
        booking.pre_reschedule_status = None
    await _add_status_log(db, booking.id, new_status, current_user["user_id"], notes)
    await db.commit()
    # -- Broadcast the transition so customer/admin live-tracking screens update --
    try:
        _trans_payload = {
            "booking_id":     str(booking.id),
            "booking_number": booking.booking_number,
            "status":         new_status.value,
            "notes":          notes,
        }
        track_task(publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED, _trans_payload))
        track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, _trans_payload))
    except Exception:
        pass

    # -- Customer push notification for key transitions --
    _CUSTOMER_PUSH_MSGS = {
        BookingStatus.ACCEPTED:   ("Technician Confirmed ✅", "Your technician has confirmed booking {num} and is preparing to visit."),
        BookingStatus.EN_ROUTE:   ("Technician On the Way 🚗", "Your technician is heading to your location for booking {num}. Track them live!"),
        BookingStatus.ARRIVED:    ("Technician Arrived 📍", "Your technician has arrived at your address for booking {num}."),
        BookingStatus.IN_PROGRESS:("Work in Progress 🔧", "Work has started on your booking {num}. We'll update you when it's done."),
        BookingStatus.COMPLETED:  ("Work Completed 🎉", "Your booking {num} is complete! Please review and proceed with payment."),
    }
    if new_status in _CUSTOMER_PUSH_MSGS and booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            _title, _body_tmpl = _CUSTOMER_PUSH_MSGS[new_status]
            _body = _body_tmpl.format(num=booking.booking_number)
            track_task(_ptc(
                db=db,
                customer_id=booking.customer_id,
                title=_title,
                body=_body,
                notif_type="BOOKING",
                data={"type": f"BOOKING_{new_status.value}", "booking_id": str(booking.id), "booking_number": booking.booking_number},
            ))
        except Exception as _pe:
            logger.warning(f"Customer push failed for {new_status.value}: {_pe}")

    return success_response(data={"status": new_status.value}, message=f"Status updated to {new_status.value}")

@router.post("/{booking_id}/accept",           summary="Accept booking [Technician]")
async def accept(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.ACCEPTED, cu, db, "Accepted by technician")

@router.post("/{booking_id}/reject",           summary="Reject booking [Technician]")
async def reject(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.PENDING, cu, db, "Rejected — reassigning")

@router.post("/{booking_id}/en-route",          summary="Technician on the way")
async def en_route(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.EN_ROUTE, cu, db, "Technician on the way")

@router.post("/{booking_id}/arrived",          summary="Technician arrived")
async def arrived(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.ARRIVED, cu, db, "Technician arrived at location")

@router.post("/{booking_id}/start-inspection", summary="Start inspection")
async def start_inspection(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.INSPECTING, cu, db, "Inspection started")

@router.post("/{booking_id}/submit-inspection", summary="Submit inspection report and start work [Technician/CCO/Admin]")
async def submit_inspection(
    booking_id: UUID,
    payload: SubmitInspectionRequest,
    cu: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    Submits inspection findings (notes + photo URLs) and transitions the
    booking from INSPECTING → IN_PROGRESS.

    Callers:
    • Technician — submits from captain app
    • CCO / Admin — submits on behalf of technician from portal

    inspection_submitted_by is set to the caller's role so all apps can
    distinguish who performed the inspection and hide the form for the other.
    A WS INSPECTION_SUBMITTED event is broadcast so all connected clients
    (admin, CCO, captain app) refresh in real time.
    """
    import json as _json
    booking = await _get_booking_or_404(db, booking_id)
    if not booking.technician_id:
        raise HTTPException(status_code=400, detail="No technician assigned to this booking.")
    if booking.status not in (BookingStatus.INSPECTING, BookingStatus.ARRIVED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot submit inspection: booking is in {booking.status.value} status. Expected INSPECTING or ARRIVED."
        )
    # Determine who submitted — caller role wins; fallback to payload hint
    role = cu.get("role", "TECHNICIAN").upper()
    if role in {"SUPER_ADMIN", "ADMIN"}:
        submitted_by = "ADMIN"
    elif role == "CCO":
        submitted_by = "CCO"
    else:
        submitted_by = "TECHNICIAN"

    booking.inspection_notes         = payload.notes.strip() if payload.notes else None
    booking.inspection_photos        = _json.dumps(payload.photo_urls) if payload.photo_urls else None
    booking.inspection_submitted_by  = submitted_by
    booking.status = BookingStatus.IN_PROGRESS
    log_note = (
        f"Inspection submitted by {submitted_by} — "
        f"{len(payload.photo_urls)} photo(s). Notes: {payload.notes[:200] if payload.notes else 'none'}"
    )
    await _add_status_log(db, booking.id, BookingStatus.IN_PROGRESS, cu["user_id"], log_note)
    await db.commit()

    # Broadcast INSPECTION_SUBMITTED so captain app hides its form and
    # admin/CCO portals reload the inspection section in real time
    _insp_payload = {
        "booking_id":     str(booking.id),
        "booking_number": booking.booking_number,
        "submitted_by":   submitted_by,
        "actor_user_id":  cu["user_id"],
        "notes":          booking.inspection_notes,
        "photo_count":    len(payload.photo_urls),
        "new_status":     BookingStatus.IN_PROGRESS.value,
    }
    track_task(publish_event(booking_room(str(booking.id)), WSEvent.INSPECTION_SUBMITTED, _insp_payload))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.INSPECTION_SUBMITTED, _insp_payload))

    return success_response(
        data={
            "status":           BookingStatus.IN_PROGRESS.value,
            "submitted_by":     submitted_by,
            "inspection_notes": booking.inspection_notes,
            "photo_count":      len(payload.photo_urls),
        },
        message=f"Inspection submitted by {submitted_by} — work started",
    )

@router.post("/{booking_id}/start-work",       summary="Start work")
async def start_work(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.IN_PROGRESS, cu, db, "Work started")

@router.post("/{booking_id}/visiting-charge", summary="Initiate visiting charge — technician arrived but customer declined repair [Technician]")
async def initiate_visiting_charge(
    booking_id: UUID,
    payload: VisitingChargeRequest,
    cu: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    When a technician has arrived / inspected but the customer does not want the repair:
    1. Creates a special VISITING CHARGE quotation (auto-approved, service item = 'Visiting Charge')
    2. Generates an invoice directly from it
    3. Sets booking status → PENDING_VERIFICATION (awaiting admin to close as CLOSED)

    Expected payload:
        { "amount": 150.0, "notes": "Customer declined repair after inspection" }
    """
    import json as _json
    from datetime import datetime as _dt
    from app.models.quotation import Quotation as QuotationModel, QuotationStatus, QuotationServiceItem
    from app.models.invoice import Invoice, InvoiceStatus, InvoiceType

    # ── Validate booking ──────────────────────────────────────────────────────
    booking = await _get_booking_or_404(db, booking_id)
    allowed_statuses = (
        BookingStatus.ARRIVED,
        BookingStatus.INSPECTING,
        BookingStatus.IN_PROGRESS,
        BookingStatus.QUOTATION_APPROVED,
    )
    if booking.status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Visiting charge can only be initiated when status is ARRIVED, INSPECTING, IN_PROGRESS, or QUOTATION_APPROVED. Current: {booking.status.value}",
        )

    # ── Validate amount ───────────────────────────────────────────────────────
    amount = float(payload.amount or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Visiting charge amount must be greater than zero.")

    notes = str(payload.notes or "Customer declined repair — visiting charge applied").strip()

    # ── Guard: no approved quotation should exist (that would mean customer agreed) ──
    from app.models.quotation import Quotation as _Q, QuotationStatus as _QS
    approved_q = (await db.execute(
        select(_Q).where(
            _Q.booking_id == booking.id,
            _Q.is_active == True,
            _Q.status == _QS.APPROVED,
        )
    )).scalars().first()
    if approved_q:
        raise HTTPException(
            status_code=400,
            detail="A quotation is already approved for this booking — use the normal repair workflow to complete work.",
        )

    # ── Number helpers ────────────────────────────────────────────────────────
    def _qnum():
        return "QTN" + _dt.utcnow().strftime("%Y%m%d%H%M%S%f")[-12:]

    def _inum():
        suffix = _dt.utcnow().strftime("%Y%m%d%H%M%S%f")[-10:]
        return f"INV{suffix}"

    # ── Create visiting-charge quotation (pre-approved) ───────────────────────
    tax_amount = round(amount * 0.18)
    total = round(amount + tax_amount)

    quotation = QuotationModel(
        quotation_number=_qnum(),
        booking_id=booking.id,
        domain_id=booking.domain_id,
        created_by=UUID(cu["user_id"]),
        status=QuotationStatus.APPROVED,          # auto-approved
        labour_charges=0.0,
        service_charges=amount,
        services_total=amount,
        parts_total=0.0,
        discount_amount=0.0,
        adjustment_amount=0.0,
        subtotal_amount=amount,
        tax_percent=18.0,
        tax_amount=tax_amount,
        total_amount=total,
        tax_mode="B2C",
        remarks=f"Visiting Charge — {notes}",
        submitted_at=_dt.utcnow(),
        approved_at=_dt.utcnow(),
        approved_by=UUID(cu["user_id"]),
    )
    db.add(quotation)
    await db.flush()  # get quotation.id

    # Service line item
    svc_item = QuotationServiceItem(
        quotation_id=quotation.id,
        service_name="Visiting Charge",
        appliance_label=notes,          # store reason in appliance_label (nullable text)
        quantity=1,
        unit_price=amount,
        total_price=amount,
        is_pending_verify=0,
    )
    db.add(svc_item)

    # ── Generate invoice directly ─────────────────────────────────────────────
    invoice = Invoice(
        invoice_number=_inum(),
        booking_id=booking.id,
        domain_id=booking.domain_id,
        quotation_id=quotation.id,
        generated_by=UUID(cu["user_id"]),
        invoice_type=InvoiceType.GST_B2C,
        status=InvoiceStatus.GENERATED,
        taxable_amount=amount,
        cgst_amount=round(tax_amount / 2),
        sgst_amount=round(tax_amount / 2),
        igst_amount=0.0,
        total_amount=total,
        balance_amount=total,
        notes=f"Visiting Charge — {notes}",
    )
    db.add(invoice)
    await db.flush()

    # Mark quotation as converted
    quotation.status = QuotationStatus.CONVERTED_TO_INVOICE

    # ── Update booking → PENDING_VERIFICATION ────────────────────────────────
    booking.status = BookingStatus.PENDING_VERIFICATION
    booking.total_amount = total
    booking.base_amount = amount
    booking.gst_amount = tax_amount
    booking.cancelled_reason = notes  # store reason in cancelled_reason for visibility

    await _add_status_log(
        db, booking.id, BookingStatus.PENDING_VERIFICATION, cu["user_id"],
        f"Visiting charge initiated — ₹{total:.2f}. Awaiting admin verification. {notes}"
    )

    await db.commit()

    # Broadcast so admin / customer live screens update
    try:
        await publish_event(booking_room(str(booking.id)), WSEvent.BOOKING_STATUS_CHANGED, {
            "booking_id": str(booking.id),
            "status": BookingStatus.PENDING_VERIFICATION.value,
        })
        await publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, {
            "booking_id": str(booking.id),
            "status": BookingStatus.PENDING_VERIFICATION.value,
        })
    except Exception:
        pass
    # Notify customer about visiting charge invoice
    if booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            track_task(_ptc(db=db, customer_id=booking.customer_id,
                title="Visiting Charge Applied 🧾",
                body=f"A visiting charge of ₹{total:.0f} has been applied to booking {booking.booking_number}. Tap to view your invoice.",
                notif_type="PAYMENT",
                data={"type": "VISITING_CHARGE", "booking_id": str(booking.id), "booking_number": booking.booking_number, "invoice_id": str(invoice.id)}))
        except Exception: pass

    return success_response(
        data={
            "status": BookingStatus.PENDING_VERIFICATION.value,
            "quotation_id": str(quotation.id),
            "quotation_number": quotation.quotation_number,
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "amount": amount,
            "tax_amount": tax_amount,
            "total_amount": total,
        },
        message=f"Visiting charge of ₹{total:.2f} initiated — booking sent for admin verification",
    )


@router.post("/{booking_id}/complete-work",    summary="Complete work")
async def complete_work(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    """
    Complete work on a booking.
    Business rule: All APPROVED quotations must have been converted to invoice (CONVERTED_TO_INVOICE)
    or explicitly rejected (REJECTED) before the booking can be marked COMPLETED.
    This ensures no approved quotation is silently abandoned.
    """
    from app.models.quotation import Quotation, QuotationStatus as QStatus
    booking = await _get_booking_or_404(db, booking_id)
    # Check for any APPROVED quotations that haven't been invoiced or rejected yet
    pending_approved = (await db.execute(
        select(Quotation).where(
            Quotation.booking_id == booking.id,
            Quotation.is_active == True,
            Quotation.status == QStatus.APPROVED,
        )
    )).scalars().all()
    if pending_approved:
        nums = ", ".join(q.quotation_number for q in pending_approved)
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete work: {len(pending_approved)} approved quotation(s) still pending invoice: {nums}. Generate invoice or reject each approved quotation first."
        )
    return await _transition(booking_id, BookingStatus.COMPLETED, cu, db, "Work completed")

# ── TIMELINE ───────────────────────────────────────────────────
@router.get("/{booking_id}/timeline", summary="Booking status timeline")
async def booking_timeline(booking_id: UUID, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    logs = (await db.execute(
        select(BookingStatusLog).where(BookingStatusLog.booking_id == booking_id).order_by(BookingStatusLog.created_at)
    )).scalars().all()
    return success_response(data=[{"status": l.status.value, "notes": l.notes, "at": iso(l.created_at)} for l in logs])


@router.get("/{booking_id}/tracking", summary="Booking live tracking")
async def booking_tracking(booking_id: UUID, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    if current_user["role"] == "CUSTOMER":
        customer = (await db.execute(select(Customer).where(Customer.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
        if not customer or customer.id != booking.customer_id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif current_user["role"] == "TECHNICIAN":
        technician = (await db.execute(select(Technician).where(Technician.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
        if not technician or technician.id != booking.technician_id:
            raise HTTPException(status_code=403, detail="Access denied")

    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id))).scalar_one_or_none()
    location = None
    if booking.technician_id:
        location = (
            await db.execute(
                select(TrackingLocation)
                .where(
                    TrackingLocation.technician_id == booking.technician_id,
                    TrackingLocation.booking_id == booking.id,
                    TrackingLocation.is_active == True,
                )
                .order_by(TrackingLocation.recorded_at.desc(), TrackingLocation.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if not location:
            location = (
                await db.execute(
                    select(TrackingLocation)
                    .where(
                        TrackingLocation.technician_id == booking.technician_id,
                        TrackingLocation.is_active == True,
                    )
                    .order_by(TrackingLocation.recorded_at.desc(), TrackingLocation.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
    return success_response(
        data={
            "booking_id": str(booking.id),
            "booking_number": booking.booking_number,
            "status": booking.status.value,
            "destination": (
                {
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                    "city": address.city,
                }
                if address
                else None
            ),
            "current_location": (
                {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "accuracy": location.accuracy,
                    "speed": location.speed,
                    "heading": location.heading,
                    "recorded_at": iso(location.recorded_at),
                }
                if location
                else None
            ),
        }
    )

# ── PAUSE WORK ─────────────────────────────────────────────────
@router.post("/{booking_id}/pause-work", summary="Pause work on booking [Technician/Admin]")
async def pause_work(
    booking_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status != BookingStatus.WORK_STARTED:
        raise HTTPException(status_code=400, detail=f"Cannot pause work: booking is in {booking.status.value} state")
    booking.status = BookingStatus.WORK_PAUSED
    await _add_status_log(db, booking.id, BookingStatus.WORK_PAUSED, current_user["user_id"], "Work paused")
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Work paused")

# ── RESUME WORK ────────────────────────────────────────────────
@router.post("/{booking_id}/resume-work", summary="Resume work on booking [Technician/Admin]")
async def resume_work(
    booking_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status != BookingStatus.WORK_PAUSED:
        raise HTTPException(status_code=400, detail=f"Cannot resume: booking is in {booking.status.value} state")
    booking.status = BookingStatus.WORK_STARTED
    await _add_status_log(db, booking.id, BookingStatus.WORK_STARTED, current_user["user_id"], "Work resumed")
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Work resumed")


# ── PUBLIC BOOKING (no auth — for domain website booking forms) ────────────────
from datetime import datetime as _dt

def _generate_customer_code():
    return "CUS" + ''.join(random.choices(string.digits, k=6))


class PublicBookingRequest(_BM):
    customer_name:   str
    mobile:          str
    email:           _Opt[str] = None
    service_name:    str
    appliance_brand: _Opt[str] = None
    appliance_model: _Opt[str] = None
    address:         str
    city:            str
    pincode:         str
    scheduled_date:  str          # "YYYY-MM-DD"
    scheduled_slot:  str          # e.g. "10:00-12:00" (canonical HH:MM-HH:MM format)
    notes:           _Opt[str] = None
    coupon_code:     _Opt[str] = None

@router.post("/public", summary="Public booking from website [No Auth]")
async def public_create_booking(
    payload: PublicBookingRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by paleisolutions (and any other domain) website booking forms.
    Finds or creates a customer record by mobile, then creates a PENDING booking
    using free-text service_name + address fields (admin resolves to FK later).
    """
    from app.utils.phone import normalize_mobile
    from app.models.user import User, UserRole

    # BUG FIX: normalize to the same canonical +91XXXXXXXXXX form used by the
    # customer app / admin dashboard / OTP login, so the same physical phone
    # number never creates a second, duplicate User+Customer pair just
    # because this form was submitted as "7894697718" instead of
    # "+917894697718".
    mobile = normalize_mobile(payload.mobile)

    customer = (await db.execute(select(Customer).where(Customer.mobile == mobile))).scalar_one_or_none()

    if not customer:
        # BUG FIX: this previously created a Customer row with no user_id at
        # all, but customers.user_id is NOT NULL + a unique FK to users.id --
        # that insert would fail for any genuinely new public-website
        # customer. Create (or reuse) the linked User first, same as the
        # admin "Add Customer" flow does.
        user = (await db.execute(select(User).where(User.mobile == mobile))).scalar_one_or_none()
        if not user:
            user = User(
                name=payload.customer_name,
                mobile=mobile,
                email=payload.email,
                role=UserRole.CUSTOMER,
                is_verified=False,
            )
            db.add(user)
            await db.flush()

        customer = Customer(
            user_id=user.id,
            name=payload.customer_name,
            mobile=mobile,
            email=payload.email,
            customer_code=_generate_customer_code(),
        )
        db.add(customer)
        await db.flush()

    try:
        sched_dt = _dt.strptime(payload.scheduled_date, "%Y-%m-%d")
    except ValueError:
        sched_dt = _dt.utcnow()

    # Resolve city_id from city name for public bookings
    from app.models.city import City as CityModel
    _pub_city_row = (await db.execute(
        select(CityModel).where(CityModel.name.ilike(payload.city), CityModel.is_active == True)
    )).scalar_one_or_none()
    pub_city_id = _pub_city_row.id if _pub_city_row else None

    booking = Booking(
        booking_number=generate_booking_number(),
        customer_id=customer.id,
        service_name=payload.service_name,
        address_line=payload.address,
        city=payload.city,
        city_id=pub_city_id,
        pincode=payload.pincode,
        scheduled_date=sched_dt,
        scheduled_slot=payload.scheduled_slot,
        notes=payload.notes,
        appliance_brand=payload.appliance_brand,
        appliance_model=payload.appliance_model,
        source=BookingSource.WEBSITE,
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    await db.flush()
    await _add_status_log(db, booking.id, BookingStatus.PENDING, notes="Public website booking")
    await db.commit()
    await db.refresh(booking)

    # ── Auto-assign if enabled — background task with own DB session ──
    track_task(_maybe_auto_assign(str(booking.id), booking.booking_number, str(booking.id)))

    # ── WS: notify admin room of new website booking ──
    _new_bkg_payload = {
        "booking_id":     str(booking.id),
        "booking_number": booking.booking_number,
        "customer_name":  customer.name,
        "customer_mobile": customer.mobile,
        "service_name":   booking.service_name,
        "city":           booking.city,
        "status":         booking.status.value,
    }
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_CREATED, _new_bkg_payload))

    return success_response(
        data={"booking_number": booking.booking_number, "id": str(booking.id)},
        message="Booking received! We will contact you shortly."
    )

# ── MARK INVOICE GENERATED ─────────────────────────────────────
@router.post("/{booking_id}/mark-invoice-generated", summary="Mark booking invoice generated [Admin/CCO]")
async def mark_invoice_generated(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status not in [BookingStatus.COMPLETED, BookingStatus.IN_PROGRESS, BookingStatus.QUOTATION_APPROVED]:
        raise HTTPException(status_code=400, detail=f"Cannot generate invoice from status: {booking.status.value}")
    booking.status = BookingStatus.INVOICE_GENERATED
    await _add_status_log(db, booking.id, BookingStatus.INVOICE_GENERATED, current_user["user_id"], "Invoice generated by admin/CCO")
    await db.commit()
    if booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            track_task(_ptc(db=db, customer_id=booking.customer_id,
                title="Invoice Ready 🧾",
                body=f"Your invoice for booking {booking.booking_number} is ready. Tap to view and pay.",
                notif_type="PAYMENT",
                data={"type": "INVOICE_GENERATED", "booking_id": str(booking.id), "booking_number": booking.booking_number}))
        except Exception: pass
    return success_response(data={"status": booking.status.value}, message="Invoice generated")

# ── MARK PAYMENT PENDING ───────────────────────────────────────
@router.post("/{booking_id}/mark-payment-pending", summary="Mark booking payment pending [Admin/CCO]")
async def mark_payment_pending(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    booking = await _get_booking_or_404(db, booking_id)
    booking.status = BookingStatus.PAYMENT_PENDING
    await _add_status_log(db, booking.id, BookingStatus.PAYMENT_PENDING, current_user["user_id"], "Payment pending — awaiting collection")
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Marked as payment pending")

# ── MARK PAID ─────────────────────────────────────────────────
@router.post("/{booking_id}/mark-paid", summary="Mark booking as fully paid [Admin/CCO]")
async def mark_paid(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    """Advance booking to PAID. Allowed from any status where invoices exist."""
    from app.models.invoice import Invoice
    from app.models.payment import PaymentTransaction, PaymentStatus
    booking = await _get_booking_or_404(db, booking_id)
    # Verify all invoices are fully paid
    invs = (await db.execute(select(Invoice).where(Invoice.booking_id == booking.id))).scalars().all()
    if not invs:
        raise HTTPException(status_code=400, detail="No invoices found for this booking.")
    total_invoiced = sum(i.total_amount or 0 for i in invs)
    total_paid = (await db.execute(
        select(func.coalesce(func.sum(PaymentTransaction.amount), 0))
        .where(PaymentTransaction.booking_id == booking.id, PaymentTransaction.status == PaymentStatus.SUCCESS)
    )).scalar_one()
    if total_paid < total_invoiced - 0.01:
        raise HTTPException(status_code=400, detail=f"Invoices not fully paid. Collected ₹{total_paid:.2f} of ₹{total_invoiced:.2f}.")
    booking.status = BookingStatus.PAID
    await _add_status_log(db, booking.id, BookingStatus.PAID, current_user["user_id"], "Full payment confirmed")
    await db.commit()
    if booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            track_task(_ptc(db=db, customer_id=booking.customer_id,
                title="Payment Confirmed ✅",
                body=f"We've received your payment for booking {booking.booking_number}. Thank you!",
                notif_type="PAYMENT",
                data={"type": "BOOKING_PAID", "booking_id": str(booking.id), "booking_number": booking.booking_number}))
        except Exception: pass
    return success_response(data={"status": booking.status.value}, message="Marked as paid")


# ── COMMISSION PREVIEW ─────────────────────────────────────────
@router.get("/{booking_id}/commission-preview", summary="Preview commission breakdown before settlement [Admin]")
async def commission_preview(
    booking_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns technician's commission group + per-service/part commission preview
    for all CONVERTED_TO_INVOICE quotations on this booking.
    """
    from app.models.commission import CommissionGroup, CommissionGroupRule, CommissionGroupAssignment, CommissionGroupPartRule
    from app.models.quotation import QuotationStatus as QS
    booking = await _get_booking_or_404(db, booking_id)

    # Get technician and their commission group
    tech = None
    group = None
    group_service_rules = []
    group_part_rules = []
    if booking.technician_id:
        tech = (await db.execute(select(Technician).where(Technician.id == booking.technician_id))).scalar_one_or_none()
        assign = (await db.execute(
            select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == booking.technician_id)
        )).scalars().all()
        if assign:
            grp_id = assign[0].group_id
            group = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == grp_id))).scalar_one_or_none()
            if group:
                group_service_rules = (await db.execute(
                    select(CommissionGroupRule).where(CommissionGroupRule.group_id == grp_id)
                )).scalars().all()
                group_part_rules = (await db.execute(
                    select(CommissionGroupPartRule).where(CommissionGroupPartRule.group_id == grp_id)
                )).scalars().all()

    # Get all invoiced quotations + their items
    quotations = (await db.execute(
        select(QuotationModel).where(
            QuotationModel.booking_id == booking.id,
            QuotationModel.status == QS.CONVERTED_TO_INVOICE,
        )
    )).scalars().all()

    line_items = []
    for q in quotations:
        svc_items = (await db.execute(
            select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == q.id, QuotationServiceItem.is_active == True)
        )).scalars().all()
        part_items = (await db.execute(
            select(QuotationPartItem).where(QuotationPartItem.quotation_id == q.id, QuotationPartItem.is_active == True)
        )).scalars().all()

        for si in svc_items:
            # Find matching commission rule
            matched_rule = next((r for r in group_service_rules if str(r.service_id) == str(si.service_id)), None)
            if matched_rule:
                if matched_rule.commission_type == "PERCENTAGE":
                    comm = int(round(si.total_price * matched_rule.rate / 100))
                else:
                    comm = int(round(matched_rule.rate * si.quantity))
                match_status = "group"
            else:
                comm = None
                match_status = "unmatched"
            line_items.append({
                "type": "SERVICE",
                "quotation_number": q.quotation_number,
                "service_id": str(si.service_id),
                "name": si.service_name,
                "quantity": si.quantity,
                "unit_price": si.unit_price,
                "total_price": si.total_price,
                "part_source": None,
                "commission_type": matched_rule.commission_type if matched_rule else None,
                "rate": matched_rule.rate if matched_rule else None,
                "commission_amount": comm,
                "matched": matched_rule is not None,
                "match_status": match_status,
            })

        for pi in part_items:
            src = pi.part_source.value if pi.part_source else "OFFICE_STOCK"
            purchase_cost = (pi.purchase_price or 0) * pi.quantity  # total cost technician paid
            # Find matching part rule: source filter matches or is NULL
            matched_rule = next((
                r for r in group_part_rules
                if (r.part_source_filter is None or r.part_source_filter == src)
                and (r.part_name_match is None or r.part_name_match.lower() in pi.part_name.lower())
            ), None)
            if matched_rule:
                if matched_rule.commission_type == "PERCENTAGE":
                    # Both OFFICE_STOCK and MARKET_PURCHASE: commission = rate% of PROFIT (selling - cost).
                    # For MARKET_PURCHASE the purchase cost is also reimbursed separately.
                    purchase_unit = pi.purchase_price or 0
                    profit = pi.unit_price - purchase_unit
                    profit_total = max(profit, 0) * pi.quantity
                    comm = int(round(profit_total * matched_rule.rate / 100))
                else:  # FLAT
                    comm = int(round(matched_rule.rate * pi.quantity))
                match_status = "group"
            else:
                comm = None
                match_status = "unmatched"
            # For MARKET_PURCHASE: technician also gets purchase cost back (separate reimbursement)
            reimb = int(round(purchase_cost)) if src == "MARKET_PURCHASE" else 0
            line_items.append({
                "type": "PART",
                "quotation_number": q.quotation_number,
                "service_id": None,
                "name": pi.part_name,
                "quantity": pi.quantity,
                "unit_price": pi.unit_price,
                "purchase_price": pi.purchase_price,
                "total_price": pi.total_price,
                "part_source": src,
                "commission_type": matched_rule.commission_type if matched_rule else None,
                "rate": matched_rule.rate if matched_rule else None,
                "commission_amount": comm,
                "purchase_reimbursement": reimb,  # ₹ paid back to tech for market parts
                "matched": matched_rule is not None,
                "match_status": match_status,
            })

    # Total payout = commission on all items + purchase reimbursement on MARKET parts
    total_commission = sum(item["commission_amount"] for item in line_items if item["commission_amount"] is not None)
    total_reimbursement = sum(item.get("purchase_reimbursement", 0) for item in line_items)
    return success_response(data={
        "technician": {"id": str(tech.id), "name": tech.name, "user_id": str(tech.user_id)} if tech else None,
        "commission_group": {"id": str(group.id), "name": group.name} if group else None,
        "line_items": line_items,
        "total_commission": int(round(total_commission)),
        "total_reimbursement": int(round(total_reimbursement)),
        "total_payout": int(round(total_commission + total_reimbursement)),
    })


# ── SETTLE BOOKING ─────────────────────────────────────────────
class SettleLineOverride(_BM):
    item_index: int        # index in line_items from preview
    commission_amount: float

class SettleBookingRequest(_BM):
    overrides: _Opt[list] = []   # list of {item_index, commission_amount}
    notes: _Opt[str] = None

@router.post("/{booking_id}/settle", summary="Settle booking — save commissions + credit wallet [Admin]")
async def settle_booking(
    booking_id: UUID,
    payload: SettleBookingRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    """
    Final settlement:
    1. Fetches commission preview (same logic as GET /commission-preview)
    2. Applies admin overrides for unmatched items
    3. Saves Commission records per line item
    4. Credits technician wallet (get-or-create)
    5. Marks booking CLOSED
    """
    from app.models.commission import Commission, CommissionGroup, CommissionGroupRule, CommissionGroupAssignment, CommissionGroupPartRule
    from app.models.wallet import Wallet, WalletTransaction
    from app.models.quotation import QuotationStatus as QS

    booking = await _get_booking_or_404(db, booking_id)
    # Allow settlement from any active (non-cancelled, non-closed) state
    _SETTLEABLE = {
        BookingStatus.ASSIGNED, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE,
        BookingStatus.ARRIVED, BookingStatus.INSPECTING, BookingStatus.IN_PROGRESS,
        BookingStatus.WORK_STARTED, BookingStatus.WORK_PAUSED,
        BookingStatus.COMPLETED, BookingStatus.INVOICE_GENERATED,
        BookingStatus.PAYMENT_PENDING, BookingStatus.PAID,
        BookingStatus.QUOTATION_APPROVED,
        BookingStatus.PENDING_VERIFICATION,  # visiting charge flow: tech collected cash, admin verifies & closes
    }
    if booking.status not in _SETTLEABLE:
        raise HTTPException(status_code=400, detail=f"Cannot settle booking in {booking.status.value} state.")

    # Block settlement if there are uncollected cash payments from technician
    from app.models.payment import CashCollectionRecord, CashCollectionStatus
    pending_cash = (await db.execute(
        select(func.count(CashCollectionRecord.id)).where(
            CashCollectionRecord.booking_id == booking_id,
            CashCollectionRecord.status == CashCollectionStatus.PENDING,
            CashCollectionRecord.is_active == True,
        )
    )).scalar_one()
    if pending_cash > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot settle: {pending_cash} cash collection(s) by technician are still pending. "
                   "Admin must collect the cash from the technician first."
        )

    # Resolve technician
    tech = None
    group = None
    group_service_rules = []
    group_part_rules = []
    if booking.technician_id:
        tech = (await db.execute(select(Technician).where(Technician.id == booking.technician_id))).scalar_one_or_none()
        assign = (await db.execute(
            select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == booking.technician_id)
        )).scalars().all()
        if assign:
            grp_id = assign[0].group_id
            group = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == grp_id))).scalar_one_or_none()
            if group:
                group_service_rules = (await db.execute(
                    select(CommissionGroupRule).where(CommissionGroupRule.group_id == grp_id)
                )).scalars().all()
                group_part_rules = (await db.execute(
                    select(CommissionGroupPartRule).where(CommissionGroupPartRule.group_id == grp_id)
                )).scalars().all()

    # Collect all line items (same logic as preview)
    quotations = (await db.execute(
        select(QuotationModel).where(
            QuotationModel.booking_id == booking.id,
            QuotationModel.status == QS.CONVERTED_TO_INVOICE,
        )
    )).scalars().all()

    line_items = []
    for q in quotations:
        svc_items = (await db.execute(
            select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == q.id, QuotationServiceItem.is_active == True)
        )).scalars().all()
        part_items = (await db.execute(
            select(QuotationPartItem).where(QuotationPartItem.quotation_id == q.id, QuotationPartItem.is_active == True)
        )).scalars().all()
        for si in svc_items:
            matched = next((r for r in group_service_rules if str(r.service_id) == str(si.service_id)), None)
            if matched:
                comm = int(round(si.total_price * matched.rate / 100)) if matched.commission_type == "PERCENTAGE" else int(round(matched.rate * si.quantity))
            else:
                comm = None
            line_items.append({"type": "SERVICE", "name": si.service_name, "quantity": si.quantity,
                                "total_price": si.total_price, "part_source": None,
                                "commission_type": matched.commission_type if matched else "PERCENTAGE",
                                "rate": matched.rate if matched else 0, "commission_amount": comm,
                                "is_repeat_complaint": bool(getattr(si, "is_repeat_complaint", False)),
                                "appliance_label": getattr(si, "appliance_label", None)})
        for pi in part_items:
            src = pi.part_source.value if pi.part_source else "OFFICE_STOCK"
            purchase_cost = (pi.purchase_price or 0) * pi.quantity  # total out-of-pocket for technician
            matched = next((r for r in group_part_rules
                            if (r.part_source_filter is None or r.part_source_filter == src)
                            and (r.part_name_match is None or r.part_name_match.lower() in pi.part_name.lower())), None)
            if matched:
                if matched.commission_type == "PERCENTAGE":
                    # Both sources: commission = rate% of PROFIT (selling price - purchase cost)
                    # MARKET_PURCHASE: purchase cost also reimbursed separately below
                    purchase_unit = pi.purchase_price or 0
                    profit = pi.unit_price - purchase_unit
                    profit_total = max(profit, 0) * pi.quantity
                    comm = int(round(profit_total * matched.rate / 100))
                else:  # FLAT
                    comm = int(round(matched.rate * pi.quantity))
            else:
                comm = None
            # MARKET_PURCHASE: reimbursement = purchase cost technician paid from own pocket
            reimb = int(round(purchase_cost)) if src == "MARKET_PURCHASE" else 0
            line_items.append({"type": "PART", "name": pi.part_name, "quantity": pi.quantity,
                                "unit_price": pi.unit_price, "purchase_price": pi.purchase_price,
                                "total_price": pi.total_price, "part_source": src,
                                "commission_type": matched.commission_type if matched else "PERCENTAGE",
                                "rate": matched.rate if matched else 0, "commission_amount": comm,
                                "purchase_reimbursement": reimb,
                                "is_repeat_complaint": bool(getattr(pi, "is_repeat_complaint", False)),
                                "appliance_label": None})

    # Apply admin overrides
    override_map = {o["item_index"]: o["commission_amount"] for o in (payload.overrides or [])}
    for idx, item in enumerate(line_items):
        if idx in override_map:
            item["commission_amount"] = override_map[idx]
        if item["commission_amount"] is None:
            item["commission_amount"] = 0  # default to 0 if still unmatched

    # ── Repeat-complaint penalty / cross-technician compensation ─────────────
    # Repeat-complaint items are free to the customer (excluded from invoice
    # totals in _recalculate_quotation), so the assigned technician does NOT
    # earn normal commission on them here — that commission instead becomes a
    # penalty debited from whoever did the ORIGINAL job (resolved via
    # QuotationAppliance.repeat_booking_id → original Booking.technician_id):
    #   - same technician both times → penalty debited from them, no credit
    #     to anyone (they're covering their own comeback).
    #   - different technician on the repeat visit → same penalty debited
    #     from the original technician, and credited to the current
    #     technician as compensation for doing the free redo work.
    # New services/parts added during the repeat visit are NOT flagged
    # is_repeat_complaint, so they still earn normal commission as usual.
    repeat_items = [item for item in line_items if item["is_repeat_complaint"]]
    normal_items = [item for item in line_items if not item["is_repeat_complaint"]]
    total_commission = sum(item["commission_amount"] for item in normal_items)
    repeat_penalty_total = sum(item["commission_amount"] for item in repeat_items)

    original_tech = None
    if repeat_items and booking.technician_id:
        qapp_rows = (await db.execute(
            select(QuotationAppliance).where(
                QuotationAppliance.quotation_id.in_([q.id for q in quotations]),
                QuotationAppliance.is_repeat_complaint == True,
                QuotationAppliance.repeat_booking_id != None,
            )
        )).scalars().all()
        if qapp_rows:
            orig_booking = (await db.execute(
                select(Booking).where(Booking.id == qapp_rows[0].repeat_booking_id)
            )).scalar_one_or_none()
            if orig_booking and orig_booking.technician_id:
                original_tech = (await db.execute(
                    select(Technician).where(Technician.id == orig_booking.technician_id)
                )).scalar_one_or_none()

    async def _wallet_txn(technician, amount, txn_type, description):
        """Credit (positive) or debit (negative-signed via txn_type) a technician's wallet."""
        w = (await db.execute(select(Wallet).where(Wallet.technician_id == technician.id))).scalar_one_or_none()
        if not w:
            w = Wallet(technician_id=technician.id, user_id=technician.user_id, balance=0.0, total_earned=0.0, total_withdrawn=0.0)
            db.add(w)
            await db.flush()
        before = w.balance or 0
        if txn_type == "DEBIT":
            w.balance = before - amount
        else:
            w.balance = before + amount
            w.total_earned = (w.total_earned or 0) + amount
        db.add(WalletTransaction(
            wallet_id=w.id, transaction_type=txn_type, amount=amount,
            balance_before=before, balance_after=w.balance,
            reference_id=str(booking.id), description=description,
        ))

    if repeat_penalty_total > 0 and original_tech:
        same_tech = tech and str(original_tech.id) == str(tech.id)
        await _wallet_txn(
            original_tech, repeat_penalty_total, "DEBIT",
            f"Repeat-complaint penalty for booking {booking.booking_number} — "
            f"free redo work does not earn commission; cost passed to technician "
            f"who did the original job." + ("" if same_tech else f" Redo performed by {tech.name if tech else 'another technician'}."),
        )
        for item in repeat_items:
            db.add(Commission(
                technician_id=original_tech.id, booking_id=booking.id,
                base_amount=item["total_price"], commission_amount=-item["commission_amount"],
                status="APPROVED", item_type="PENALTY", item_name=item["name"],
                item_quantity=item["quantity"], part_source=item["part_source"],
                notes=f"Repeat-complaint penalty on {item['name']} (originally serviced by this technician).",
            ))
        if not same_tech and tech:
            # Different technician performed the free redo — compensate them.
            await _wallet_txn(
                tech, repeat_penalty_total, "CREDIT",
                f"Repeat-complaint compensation for booking {booking.booking_number} — "
                f"free redo work funded by penalty on original technician {original_tech.name}.",
            )
            for item in repeat_items:
                db.add(Commission(
                    technician_id=tech.id, booking_id=booking.id,
                    base_amount=item["total_price"], commission_amount=item["commission_amount"],
                    status="APPROVED", item_type="REPEAT_COMPENSATION", item_name=item["name"],
                    item_quantity=item["quantity"], part_source=item["part_source"],
                    notes=f"Repeat-complaint redo compensation (penalty funded from {original_tech.name}).",
                ))
    elif repeat_items:
        # Repeat items exist but no original technician could be resolved
        # (e.g. original booking/technician missing) — log for audit, don't
        # silently pay commission on free-to-customer work.
        for item in repeat_items:
            db.add(Commission(
                technician_id=(tech.id if tech else None), booking_id=booking.id,
                base_amount=item["total_price"], commission_amount=0,
                status="APPROVED", item_type=item["type"], item_name=item["name"],
                item_quantity=item["quantity"], part_source=item["part_source"],
                notes=f"Repeat-complaint item — original technician could not be resolved; no commission/penalty applied.",
            ))

    # Save Commission records per line (normal, non-repeat items only)
    # For MARKET_PURCHASE parts: two records — PURCHASE_REIMBURSEMENT + commission on profit.
    # For OFFICE_STOCK parts / services: one record — commission on profit only.
    if tech:
        for item in normal_items:
            # 1. Main commission record (profit commission or manual override)
            db.add(Commission(
                technician_id=tech.id,
                booking_id=booking.id,
                base_amount=item["total_price"],
                commission_amount=item["commission_amount"],
                status="PENDING",  # Wallet credited only after admin confirms payment on Commissions page
                item_type=item["type"],
                item_name=item["name"],
                item_quantity=item["quantity"],
                part_source=item["part_source"],
                notes=(
                    f"Settled: {item['commission_type']} {item['rate']}% profit commission on {item['name']}"
                    if item.get("rate") else f"Manual override: ₹{item['commission_amount']}"
                ),
            ))
            # 2. Purchase reimbursement for MARKET_PURCHASE parts (tech paid from own pocket)
            reimb = item.get("purchase_reimbursement", 0) or 0
            if reimb > 0:
                db.add(Commission(
                    technician_id=tech.id,
                    booking_id=booking.id,
                    base_amount=item["total_price"],
                    commission_amount=reimb,
                    status="PENDING",
                    item_type="PURCHASE_REIMBURSEMENT",
                    item_name=item["name"],
                    item_quantity=item["quantity"],
                    part_source=item["part_source"],
                    notes=f"Market part purchase reimbursement: ₹{item.get('purchase_price', 0)} × {item['quantity']} unit(s) — {item['name']}",
                ))
        # NOTE: Wallet is NOT credited here. The technician's wallet is credited
        # only when admin clicks "Mark Paid" (Confirm Payment) on the Commissions page.

    # Mark booking CLOSED
    booking.status = BookingStatus.CLOSED
    _penalty_note = f" Repeat-complaint penalty: ₹{repeat_penalty_total:.2f} (from {original_tech.name})." if (repeat_penalty_total > 0 and original_tech) else ""
    settlement_note = f"Settled by {current_user.get('email', 'admin')}. Commission: ₹{total_commission:.2f}.{_penalty_note} {payload.notes or ''}".strip()
    await _add_status_log(db, booking.id, BookingStatus.CLOSED, current_user["user_id"], settlement_note)
    await db.commit()

    # ── WebSocket: notify admin + technician rooms ───────────────────────
    _settle_payload = {
        "booking_id":     str(booking_id),
        "booking_number": booking.booking_number,
        "status":         booking.status.value,
        "total_commission": total_commission,
    }
    from app.websocket.manager import ADMIN_BOOKINGS_ROOM, booking_room, technician_room, WSEvent
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, _settle_payload))
    track_task(publish_event(booking_room(str(booking_id)), WSEvent.BOOKING_STATUS_CHANGED, _settle_payload))
    # Also fire PAYMENT_RECEIVED for notification system
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.PAYMENT_RECEIVED, _settle_payload))
    # ── Notify technician (FCM + notification record) ─────────────────────
    if tech:
        track_task(push_to_technician(
            db=db, technician=tech,
            title="Booking Settled 🎉",
            body=f"Booking {booking.booking_number} has been settled. Commission ₹{total_commission:.2f} credited to your wallet.",
            notif_type="PAYMENT",
            data={"type": "BOOKING_SETTLED", "booking_id": str(booking_id)},
        ))
        track_task(publish_event(technician_room(str(tech.id)), WSEvent.BOOKING_STATUS_CHANGED, _settle_payload))
    return success_response(
        data={
            "status": booking.status.value,
            "total_commission": total_commission,
            "line_items_count": len(line_items),
            "wallet_credited": bool(tech),
            "settlement_note": settlement_note,
        },
        message="Booking settled successfully"
    )


# ── CLOSE / SETTLE BOOKING (legacy simple) ─────────────────────
@router.post("/{booking_id}/close", summary="Close booking [Admin]")
async def close_booking(
    booking_id: UUID,
    commission_pct: float = 20.0,
    notes: str = None,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    """Legacy simple close — use /settle for full commission engine."""
    booking = await _get_booking_or_404(db, booking_id)
    if booking.status not in [BookingStatus.PAID, BookingStatus.PAYMENT_PENDING, BookingStatus.COMPLETED, BookingStatus.INVOICE_GENERATED, BookingStatus.PENDING_VERIFICATION]:
        raise HTTPException(status_code=400, detail=f"Cannot close booking in {booking.status.value} state.")
    booking.status = BookingStatus.CLOSED
    settlement_note = f"Closed by {current_user.get('email', 'admin')}. {notes or ''}".strip()
    await _add_status_log(db, booking.id, BookingStatus.CLOSED, current_user["user_id"], settlement_note)
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Booking closed")

# ── APPROVE QUOTATION ON BEHALF (Admin/CCO) ────────────────────
@router.post("/{booking_id}/approve-quotation", summary="Admin approves quotation and advances status [Admin/CCO]")
async def approve_quotation_for_booking(
    booking_id: UUID,
    quotation_id: str = None,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    """
    Admin/CCO approves the booking's quotation and transitions booking to QUOTATION_APPROVED status.
    If quotation_id is given, also approves that specific quotation via the quotations module.
    """
    booking = await _get_booking_or_404(db, booking_id)
    # A quotation cannot be approved until a technician is assigned to the booking —
    # approval kicks off the repair workflow (inspection/work/invoice).
    if not booking.technician_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot approve quotation: no technician assigned to this booking yet. Assign a technician first."
        )
    # Approve the quotation model if given
    if quotation_id:
        from app.models.quotation import Quotation as QuotationModel, QuotationStatus
        from uuid import UUID as _UUID
        q = (await db.execute(select(QuotationModel).where(QuotationModel.id == _UUID(quotation_id)))).scalar_one_or_none()
        if q:
            q.status = QuotationStatus.APPROVED
            q.approved_by_id = _UUID(current_user["user_id"])
    booking.status = BookingStatus.QUOTATION_APPROVED
    await _add_status_log(db, booking.id, BookingStatus.QUOTATION_APPROVED, current_user["user_id"],
                         f"Quotation approved by admin/CCO ({current_user.get('email', '')}). Technician can now start repair.")
    await db.commit()
    if booking.customer_id:
        try:
            from app.utils.notify import push_to_customer as _ptc
            track_task(_ptc(db=db, customer_id=booking.customer_id,
                title="Quotation Approved ✅",
                body=f"Your quotation for booking {booking.booking_number} has been approved. Repair will begin shortly.",
                notif_type="BOOKING",
                data={"type": "QUOTATION_APPROVED", "booking_id": str(booking.id), "booking_number": booking.booking_number}))
        except Exception: pass
    return success_response(data={"status": booking.status.value}, message="Quotation approved — technician can now start repair")



async def _get_booking_customer_rating(db, booking_id):
    """Returns {rating, review} dict if customer has rated this booking, else None."""
    from app.models.technician import TechnicianRating
    row = (await db.execute(
        select(TechnicianRating.rating, TechnicianRating.review)
        .where(TechnicianRating.booking_id == booking_id)
    )).first()
    if not row:
        return None
    return {"rating": row.rating, "review": row.review}


# ── CUSTOMER RATE BOOKING ──────────────────────────────────────
@router.post("/{booking_id}/rate", summary="Customer rates technician after completion")
async def rate_booking(
    booking_id: UUID,
    payload: dict,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    Customer submits a star rating (1-5) and optional review text for a
    completed booking.  Persists into technician_ratings and updates the
    technician's rolling average rating on the technicians table.

    Only the booking's own customer may call this; only COMPLETED / PAID /
    CLOSED / SETTLED bookings are eligible.  A booking can only be rated
    once — a second call returns 400.
    """
    from app.models.technician import TechnicianRating, Technician as TechModel
    from sqlalchemy import delete as sa_delete

    rating_val = payload.get("rating")
    review_text = payload.get("review", "")

    if rating_val is None or not (1 <= float(rating_val) <= 5):
        raise HTTPException(status_code=422, detail="rating must be between 1 and 5")

    booking = await _get_booking_or_404(db, booking_id)

    # Ownership check — customers can only rate their own bookings
    if current_user.get("role") == "CUSTOMER":
        cust = (await db.execute(
            select(Customer).where(Customer.user_id == UUID(current_user["user_id"]))
        )).scalar_one_or_none()
        if not cust or booking.customer_id != cust.id:
            raise HTTPException(status_code=403, detail="Not your booking")

    # Only rate completed/paid/closed/settled bookings
    rateable = {BookingStatus.COMPLETED, BookingStatus.PAID, BookingStatus.CLOSED, BookingStatus.SETTLED}
    if booking.status not in rateable:
        raise HTTPException(status_code=400, detail=f"Booking cannot be rated in status {booking.status.value}")

    if not booking.technician_id:
        raise HTTPException(status_code=400, detail="No technician assigned to this booking")

    # Idempotency — only one rating per booking
    existing = (await db.execute(
        select(TechnicianRating).where(TechnicianRating.booking_id == booking_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="This booking has already been rated")

    # Persist the rating row
    cust_row = (await db.execute(
        select(Customer).where(Customer.user_id == UUID(current_user["user_id"]))
    )).scalar_one_or_none() if current_user.get("role") == "CUSTOMER" else None

    new_rating = TechnicianRating(
        technician_id=booking.technician_id,
        booking_id=booking_id,
        customer_id=cust_row.id if cust_row else None,
        rating=float(rating_val),
        review=review_text or None,
    )
    db.add(new_rating)

    # Update technician's rolling average rating
    tech = (await db.execute(
        select(TechModel).where(TechModel.id == booking.technician_id)
    )).scalar_one_or_none()
    if tech:
        total_count = (await db.execute(
            select(func.count(TechnicianRating.id)).where(
                TechnicianRating.technician_id == booking.technician_id
            )
        )).scalar_one() or 0
        total_sum = (await db.execute(
            select(func.sum(TechnicianRating.rating)).where(
                TechnicianRating.technician_id == booking.technician_id
            )
        )).scalar_one() or 0.0
        # +1 for the new row being added
        new_avg = (total_sum + float(rating_val)) / (total_count + 1)
        tech.rating = round(new_avg, 2)

    await db.commit()
    return success_response(
        data={"rating": float(rating_val), "review": review_text},
        message="Thank you for your feedback!"
    )


# ── REPORT ISSUE (repeat complaint, within 10 days of closure) ─────────────
REPEAT_COMPLAINT_WINDOW_DAYS = 10

class ReportIssueRequest(_BM):
    notes: str
    scheduled_date: _Opt[str] = None   # YYYY-MM-DD, defaults to today
    scheduled_slot: _Opt[str] = None

@router.post("/{booking_id}/report-issue", summary="Customer reports a follow-up issue within 10 days of closure — creates a repeat-complaint booking pre-assigned to the same technician")
async def report_issue(
    booking_id: UUID,
    payload: ReportIssueRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone

    orig = await _get_booking_or_404(db, booking_id)

    # Only the owning customer, or admin/CCO on the customer's behalf, may report
    if current_user["role"] == "CUSTOMER":
        cust = (await db.execute(select(Customer).where(Customer.user_id == UUID(current_user["user_id"])))).scalar_one_or_none()
        if not cust or orig.customer_id != cust.id:
            raise HTTPException(status_code=403, detail="Not your booking")
    elif current_user["role"] not in ("SUPER_ADMIN", "ADMIN", "CCO"):
        raise HTTPException(status_code=403, detail="Not authorized to report an issue")

    if orig.status not in (BookingStatus.CLOSED, BookingStatus.SETTLED, BookingStatus.PAID):
        raise HTTPException(status_code=400, detail=f"Booking must be closed before an issue can be reported (current: {orig.status.value}).")

    # 10-day eligibility window, measured from the CLOSED status log entry
    closed_log = (await db.execute(
        select(BookingStatusLog).where(
            BookingStatusLog.booking_id == orig.id,
            BookingStatusLog.status == BookingStatus.CLOSED,
        ).order_by(BookingStatusLog.created_at.desc())
    )).scalars().first()
    if not closed_log:
        raise HTTPException(status_code=400, detail="No closure record found for this booking — cannot determine the 10-day reporting window.")
    closed_at = closed_log.created_at
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days_since = (now - closed_at).days
    if days_since > REPEAT_COMPLAINT_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"This booking was closed {days_since} days ago — reports are only accepted within {REPEAT_COMPLAINT_WINDOW_DAYS} days of closure.",
        )

    # Don't allow duplicate open repeat-complaint bookings for the same original
    existing_repeat = (await db.execute(
        select(Booking).where(
            Booking.repeat_of_booking_id == orig.id,
            Booking.status.notin_([BookingStatus.CANCELLED, BookingStatus.CLOSED, BookingStatus.SETTLED]),
            Booking.is_active == True,
        )
    )).scalars().first()
    if existing_repeat:
        raise HTTPException(status_code=400, detail=f"A repeat-complaint booking ({existing_repeat.booking_number}) is already open for this issue.")

    try:
        from datetime import timezone as _tz_rc, timedelta as _td_rc
        _IST = _tz_rc(timedelta(hours=5, minutes=30))
        _today_ist = datetime.now(_tz_rc.utc).astimezone(_IST).date()
        sched_dt = datetime.strptime(payload.scheduled_date, "%Y-%m-%d") if payload.scheduled_date else datetime.combine(_today_ist, datetime.min.time())
    except ValueError:
        from datetime import timezone as _tz_rc2
        _IST2 = _tz_rc2(timedelta(hours=5, minutes=30))
        sched_dt = datetime.combine(datetime.now(_tz_rc2.utc).astimezone(_IST2).date(), datetime.min.time())

    new_booking = Booking(
        booking_number=generate_booking_number(),
        customer_id=orig.customer_id,
        technician_id=orig.technician_id,     # pre-assign the same technician who did the original job
        service_id=orig.service_id,
        address_id=orig.address_id,
        service_name=orig.service_name,
        address_line=orig.address_line,
        city=orig.city,
        city_id=orig.city_id,
        pincode=orig.pincode,
        domain_id=orig.domain_id,
        scheduled_date=sched_dt,
        scheduled_slot=payload.scheduled_slot or orig.scheduled_slot,
        notes=f"REPEAT COMPLAINT of {orig.booking_number}: {payload.notes}",
        appliance_brand=orig.appliance_brand,
        appliance_model=orig.appliance_model,
        source=orig.source,
        status=BookingStatus.ASSIGNED if orig.technician_id else BookingStatus.CONFIRMED,
        repeat_of_booking_id=orig.id,
    )
    db.add(new_booking)
    await db.flush()
    await _add_status_log(
        db, new_booking.id, new_booking.status, current_user["user_id"],
        f"Repeat-complaint booking created from {orig.booking_number} within {days_since} day(s) of closure.",
    )
    await db.commit()
    await db.refresh(new_booking)

    # Notify the pre-assigned technician + admin room
    if new_booking.technician_id:
        tech = (await db.execute(select(Technician).where(Technician.id == new_booking.technician_id))).scalar_one_or_none()
        if tech:
            await push_to_technician(
                db, tech, "Repeat Complaint — Same Job Assigned",
                f"Booking {new_booking.booking_number} is a repeat complaint for {orig.booking_number}. "
                f"Please revisit — labor is free for the repeated issue.",
                notif_type="BOOKING",
                data={"booking_id": str(new_booking.id), "repeat_of_booking_id": str(orig.id)},
            )
    else:
        # Original technician no longer available — fall back to normal auto-assign
        track_task(_maybe_auto_assign(str(new_booking.id), new_booking.booking_number, str(new_booking.id)))

    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_CREATED, {
        "booking_id": str(new_booking.id), "booking_number": new_booking.booking_number,
        "repeat_of_booking_id": str(orig.id), "repeat_of_booking_number": orig.booking_number,
        "status": new_booking.status.value,
    }))

    return success_response(
        data={
            "booking_number": new_booking.booking_number,
            "id": str(new_booking.id),
            "technician_pre_assigned": bool(new_booking.technician_id),
        },
        message="Issue reported. " + (
            "The same technician has been assigned to revisit."
            if new_booking.technician_id else "Awaiting technician assignment."
        ),
    )
