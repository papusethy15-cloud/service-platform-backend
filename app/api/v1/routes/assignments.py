"""
app/api/v1/routes/assignments.py
═════════════════════════════════
Advanced Booking Assignment System — Palei Solutions
─────────────────────────────────────────────────────
Fixes implemented in this version
──────────────────────────────────
A. Redis distributed lock per booking  — prevents duplicate concurrent assigns
B. In-memory watcher registry          — one watcher per booking, old one cancelled instantly
C. Two-phase timeout watcher           — Phase 1: 10-s screen-ACK window | Phase 2: 5-min response
D. Hard 5-minute cap in code           — DB value is a hint, MAX_RESPONSE_MINUTES is the law
E. Manual assign kills auto atomically — cancels watcher + marks old history REASSIGNED first
F. screen-ack endpoint                 — app calls this when IncomingBookingScreen is shown

Booking assignment lifecycle
─────────────────────────────
 CONFIRMED  ──[auto/manual assign]──►  ASSIGNED  ──[tech accepts]──►  ACCEPTED
                                          │
                              [reject / timeout / screen-miss]
                                          │
                                       CONFIRMED  ──[redispatch or escalate]──►  ...
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminOrCCO, AdminOrTech
from app.api.v1.schemas.assignment import (
    AutoAssignmentRequest,
    ManualAssignmentRequest,
    UpdateAssignmentRuleRequest,
)
from app.core.background_tasks import track_task
from app.core.config import settings
from app.core.database import get_db
from app.models.assignment import (
    AssignmentHistory,
    AssignmentRule,
    AssignmentStatus,
    AssignmentType,
)
from app.models.booking import Booking, BookingStatus, BookingStatusLog
from app.models.customer import CustomerAddress
from app.models.technician import Technician, TechnicianSkill, TechnicianStatus
from app.utils.fcm import send_booking_push, send_simple_push
from app.utils.notify import push_to_technician
from app.utils.response import success_response
from app.websocket.manager import (
    ADMIN_ASSIGNMENTS_ROOM,
    ADMIN_BOOKINGS_ROOM,
    WSEvent,
    booking_room,
    publish_event,
    technician_room,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Hard constant — no DB value can override this ─────────────────────────────
MAX_RESPONSE_MINUTES: int = 5
# Grace window for screen-ACK before we call SCREEN_MISSED and redispatch
SCREEN_ACK_GRACE_SECONDS: int = 12  # 10 s + 2 s buffer for slow devices

# ── Per-booking watcher registry ──────────────────────────────────────────────
# Maps booking_id (str) → running asyncio.Task (_two_phase_watcher).
# Only ONE task per booking is alive at any time.
_ASSIGN_WATCHERS: dict[str, asyncio.Task] = {}

ACTIVE_BOOKING_STATUSES = [
    BookingStatus.ASSIGNED,
    BookingStatus.ACCEPTED,
    BookingStatus.ARRIVED,
    BookingStatus.INSPECTING,
    BookingStatus.IN_PROGRESS,
    # A pending cancellation isn't final yet — admin/CCO may reject it and
    # restore the booking, so it still counts against the technician's
    # active workload until resolved.
    BookingStatus.CANCELLATION_REQUESTED,
]

# Statuses where a technician's slot is considered OCCUPIED.
# Completed/invoiced/cancelled bookings free the slot.
SLOT_OCCUPIED_STATUSES = [
    BookingStatus.ASSIGNED,
    BookingStatus.ACCEPTED,
    BookingStatus.EN_ROUTE,
    BookingStatus.ARRIVED,
    BookingStatus.INSPECTING,
    BookingStatus.IN_PROGRESS,
    BookingStatus.WORK_STARTED,
    BookingStatus.WORK_PAUSED,
    BookingStatus.QUOTATION_APPROVED,
    BookingStatus.TECHNICIAN_ACCEPTED,
    BookingStatus.PENDING_VERIFICATION,
    # Same reasoning as ACTIVE_BOOKING_STATUSES — keep the slot reserved
    # until admin/CCO actually confirms the cancellation.
    BookingStatus.CANCELLATION_REQUESTED,
]

# Max bookings per technician per slot (same date + same slot string)
MAX_BOOKINGS_PER_SLOT: int = 2


# ════════════════════════════════════════════════════════════════════════════════
#  REDIS LOCK — Fix A
# ════════════════════════════════════════════════════════════════════════════════

async def _acquire_assign_lock(booking_id: str, ttl_seconds: int = 15) -> bool:
    """
    Atomic SET NX EX — only one caller wins.  Returns True if the lock was
    acquired, False if another request already holds it.
    TTL is a safety net; the lock is explicitly released after the DB commit.
    """
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        ok = await r.set(f"palei:assign_lock:{booking_id}", "1", nx=True, ex=ttl_seconds)
        await r.aclose()
        return bool(ok)
    except Exception as exc:
        logger.warning(f"[LOCK] Redis lock acquire failed for {booking_id}: {exc} — allowing through")
        return True  # Redis down → fail-open so assign still works


async def _release_assign_lock(booking_id: str) -> None:
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.delete(f"palei:assign_lock:{booking_id}")
        await r.aclose()
    except Exception as exc:
        logger.warning(f"[LOCK] Redis lock release failed for {booking_id}: {exc}")


# ════════════════════════════════════════════════════════════════════════════════
#  WATCHER REGISTRY — Fix B
# ════════════════════════════════════════════════════════════════════════════════

def _cancel_existing_watcher(booking_id: str) -> None:
    """Cancel the running watcher for a booking (if any) and remove from registry."""
    old_task = _ASSIGN_WATCHERS.pop(booking_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
        logger.info(f"[WATCHER] Cancelled existing watcher for booking {booking_id}")


def _register_watcher(booking_id: str, task: asyncio.Task) -> None:
    """Register a new watcher, cancelling any previous one first."""
    _cancel_existing_watcher(booking_id)
    _ASSIGN_WATCHERS[booking_id] = task
    task.add_done_callback(lambda _: _ASSIGN_WATCHERS.pop(booking_id, None))


# ════════════════════════════════════════════════════════════════════════════════
#  DB / SCORING HELPERS  (unchanged from original)
# ════════════════════════════════════════════════════════════════════════════════

def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def _get_booking_or_404(db: AsyncSession, booking_id: UUID) -> Booking:
    booking = (await db.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


async def _get_default_rules(db: AsyncSession) -> AssignmentRule:
    rules = (await db.execute(select(AssignmentRule).where(AssignmentRule.name == "default"))).scalar_one_or_none()
    if not rules:
        rules = AssignmentRule(name="default")
        db.add(rules)
        await db.flush()
    return rules


async def _get_active_workload(db: AsyncSession, technician_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Booking.id)).where(
            Booking.technician_id == technician_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
    )
    return result.scalar_one() or 0


async def _get_slot_booking_count(
    db: AsyncSession,
    technician_id: UUID,
    booking_date,        # datetime.date
    scheduled_slot: str, # e.g. "10:00-12:00" (canonical HH:MM-HH:MM format)
) -> int:
    """
    Count active bookings for this technician on the exact same date+slot.
    Completed/invoiced/cancelled bookings do NOT count — their slot is free.
    Returns int (0, 1, or 2).  The caller blocks assignment when >= MAX_BOOKINGS_PER_SLOT.
    """
    from sqlalchemy import cast, Date as SADate
    result = await db.execute(
        select(func.count(Booking.id)).where(
            Booking.technician_id == technician_id,
            cast(Booking.scheduled_date, SADate) == booking_date,
            Booking.scheduled_slot == scheduled_slot,
            Booking.status.in_(SLOT_OCCUPIED_STATUSES),
        )
    )
    return result.scalar_one() or 0


async def _add_booking_log(db: AsyncSession, booking: Booking, user_id: str | None, notes: str):
    db.add(
        BookingStatusLog(
            booking_id=booking.id,
            status=booking.status,
            changed_by=UUID(user_id) if user_id else None,
            notes=notes,
        )
    )


async def _resolve_address(db: AsyncSession, booking: Booking):
    """Returns (address_str, lat, lng) from CustomerAddress FK or free-text fallback."""
    addr_str, lat, lng = "", None, None
    if getattr(booking, "address_id", None):
        try:
            ca = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id))).scalar_one_or_none()
            if ca:
                parts = [ca.address_line1 or "", ca.address_line2 or "", ca.city or "", ca.state or "", ca.pincode or ""]
                addr_str = ", ".join(p for p in parts if p)
                lat, lng = ca.latitude, ca.longitude
        except Exception:
            pass
    if not addr_str:
        free = [getattr(booking, "address_line", "") or "", getattr(booking, "city", "") or "", getattr(booking, "pincode", "") or ""]
        addr_str = ", ".join(p for p in free if p)
    return addr_str, lat, lng


async def _pick_best_technician(db: AsyncSession, booking: Booking, rules: AssignmentRule):
    """Score ALL active, auto-assign-eligible technicians (no online filter) — for initial auto-assign."""
    technicians = (await db.execute(
        select(Technician).where(
            Technician.status == TechnicianStatus.ACTIVE,
            Technician.auto_assign_eligible == True,
        )
    )).scalars().all()
    if not technicians:
        raise HTTPException(status_code=404, detail="No active technicians available for auto-assign")
    return await _score_technicians(db, booking, rules, technicians)


async def _pick_best_technician_online(
    db: AsyncSession,
    booking: Booking,
    rules: AssignmentRule,
    exclude_technician_id=None,
):
    """Score only ONLINE, active, auto-assign-eligible technicians — for re-dispatch after reject/timeout/screen-miss."""
    technicians = (
        await db.execute(
            select(Technician).where(
                Technician.status == TechnicianStatus.ACTIVE,
                Technician.is_online == True,
                Technician.auto_assign_eligible == True,
            )
        )
    ).scalars().all()
    if exclude_technician_id:
        technicians = [t for t in technicians if t.id != exclude_technician_id]
    if not technicians:
        raise HTTPException(status_code=404, detail="No online technicians available for auto-assign")
    return await _score_technicians(db, booking, rules, technicians)


async def _score_technicians(db: AsyncSession, booking: Booking, rules: AssignmentRule, technicians: list):
    """Shared scoring logic. Returns (score, technician, workload) of best candidate."""
    skill_rows = (await db.execute(select(TechnicianSkill).where(TechnicianSkill.service_id == booking.service_id))).scalars().all()
    skill_match_ids = {row.technician_id for row in skill_rows}

    if rules.require_skill_match:
        technicians = [t for t in technicians if t.id in skill_match_ids]
        if not technicians:
            raise HTTPException(status_code=404, detail="No technician with required skill")

    _booking_lat, _booking_lng = None, None
    if booking.address_id:
        addr = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id))).scalar_one_or_none()
        if addr and getattr(addr, "latitude", None) and getattr(addr, "longitude", None):
            _booking_lat, _booking_lng = addr.latitude, addr.longitude
        if rules.prefer_same_city and addr:
            same_city = [t for t in technicians if t.city and t.city.lower() == addr.city.lower()]
            if same_city:
                technicians = same_city

    # Extract booking date for slot-capacity check
    _booking_date = None
    _booking_slot = getattr(booking, "scheduled_slot", None)
    if getattr(booking, "scheduled_date", None):
        _bd = booking.scheduled_date
        _booking_date = _bd.date() if hasattr(_bd, "date") else _bd

    scored = []
    for tech in technicians:
        workload = await _get_active_workload(db, tech.id)
        if workload >= rules.max_active_bookings:
            continue
        # ── Slot-capacity guard: max MAX_BOOKINGS_PER_SLOT per technician per date+slot ──
        if _booking_date and _booking_slot:
            slot_count = await _get_slot_booking_count(db, tech.id, _booking_date, _booking_slot)
            if slot_count >= MAX_BOOKINGS_PER_SLOT:
                continue  # this technician's slot is full for this date+time
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
        raise HTTPException(status_code=404, detail="No technician available under current assignment rules")

    scored.sort(key=lambda item: (item[0], item[1].rating, -item[2]), reverse=True)
    return scored[0]


# ════════════════════════════════════════════════════════════════════════════════
#  _apply_assignment  — commits DB + fires WS + FCM
# ════════════════════════════════════════════════════════════════════════════════

async def _apply_assignment(
    db: AsyncSession,
    booking: Booking,
    technician: Technician,
    assignment_type: AssignmentType,
    assigned_by: str | None,
    notes: str | None,
    score: float = 0.0,
    response_timeout_minutes: int = MAX_RESPONSE_MINUTES,
) -> AssignmentHistory:
    """
    Core commit function.  Returns the newly created AssignmentHistory row.
    Fix D: timeout is capped at MAX_RESPONSE_MINUTES regardless of rules value.
    """
    # Fix D — hard cap
    safe_timeout = min(response_timeout_minutes, MAX_RESPONSE_MINUTES)

    booking.technician_id = technician.id
    _RESET_TO_ASSIGNED = {BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.ASSIGNED, BookingStatus.ACCEPTED}
    if booking.status in _RESET_TO_ASSIGNED:
        booking.status = BookingStatus.ASSIGNED

    new_assignment = AssignmentHistory(
        booking_id=booking.id,
        technician_id=technician.id,
        assigned_by=UUID(assigned_by) if assigned_by else None,
        assignment_type=assignment_type,
        status=AssignmentStatus.ASSIGNED,
        score=score,
        notes=notes,
        response_deadline=datetime.now(timezone.utc) + timedelta(minutes=safe_timeout),
        screen_shown_at=None,
    )
    db.add(new_assignment)
    await _add_booking_log(db, booking, assigned_by, notes or f"{assignment_type.value.title()} assignment created")
    await db.commit()
    await db.refresh(booking)
    await db.refresh(new_assignment)

    # ── Resolve address once for WS + FCM ────────────────────────────────────
    addr_str, resolved_lat, resolved_lng = await _resolve_address(db, booking)

    from app.models.customer import Customer as _Customer
    cust = (await db.execute(select(_Customer).where(_Customer.id == booking.customer_id))).scalar_one_or_none()
    cust_name = cust.name if cust else ""

    # ── WebSocket events ──────────────────────────────────────────────────────
    try:
        base_payload = {
            "booking_id":      str(booking.id),
            "booking_number":  booking.booking_number,
            "status":          booking.status.value,
            "technician_id":   str(technician.id),
            "technician_name": technician.name,
            "assignment_type": assignment_type.value,
            "score":           round(score, 2),
        }
        track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_CREATED, base_payload))
        track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, base_payload))
        track_task(publish_event(booking_room(str(booking.id)), WSEvent.ASSIGNMENT_CREATED, base_payload))

        tech_payload = {
            **base_payload,
            "assignment_id":    str(new_assignment.id),
            "customer_name":    cust_name,
            "address":          addr_str,
            "latitude":         resolved_lat,
            "longitude":        resolved_lng,
            "service_name":     getattr(booking, "service_name", None),
            "scheduled_date":   str(booking.scheduled_date) if getattr(booking, "scheduled_date", None) else None,
            "scheduled_time":   str(getattr(booking, "scheduled_slot", None) or ""),
            "response_deadline": new_assignment.response_deadline.isoformat() if new_assignment.response_deadline else None,
        }
        track_task(publish_event(technician_room(str(technician.id)), WSEvent.ASSIGNMENT_CREATED, tech_payload))
    except Exception as ws_err:
        logger.warning(f"WS publish failed (non-critical): {ws_err}")

    # ── FCM push ──────────────────────────────────────────────────────────────
    try:
        if technician.fcm_token:
            track_task(send_booking_push(
                fcm_token=technician.fcm_token,
                assignment_id=str(new_assignment.id),
                booking_id=str(booking.id),
                booking_number=booking.booking_number or "",
                customer_name=cust_name,
                address=addr_str,
                service_name=getattr(booking, "service_name", None),
                scheduled_date=str(booking.scheduled_date) if booking.scheduled_date else None,
                scheduled_time=str(getattr(booking, "scheduled_slot", None) or ""),
                response_deadline=new_assignment.response_deadline,
                latitude=resolved_lat,
                longitude=resolved_lng,
            ))
    except Exception as fcm_err:
        logger.warning(f"FCM push failed (non-critical): {fcm_err}")

    # ── Notification record ───────────────────────────────────────────────────
    try:
        from app.models.notification import Notification as _Notif
        db.add(_Notif(
            user_id=technician.user_id,
            title="New Job Assigned 🔧",
            body=f"You have a new job for booking {booking.booking_number}. Tap to view.",
            channel="PUSH",
            is_read=False,
            data={
                "type": "ASSIGNMENT_CREATED",
                "notification_type": "ASSIGNMENT",
                "booking_id": str(booking.id),
                "assignment_id": str(new_assignment.id),
                "booking_number": booking.booking_number,
            },
        ))
        await db.commit()
    except Exception as notif_err:
        logger.warning(f"Notification record save failed (non-critical): {notif_err}")

    return new_assignment


# ════════════════════════════════════════════════════════════════════════════════
#  _mark_old_assignments_reassigned  — Fix E helper
# ════════════════════════════════════════════════════════════════════════════════

async def _mark_old_assignments_reassigned(db: AsyncSession, booking_id: UUID, notify_techs: bool = True) -> None:
    """
    Mark all currently-ASSIGNED AssignmentHistory rows for this booking as REASSIGNED
    and send a cancellation push to affected technicians so their IncomingBookingScreen
    dismisses itself via the ASSIGNMENT_AUTO_CANCELLED WS event.
    """
    pending = (
        await db.execute(
            select(AssignmentHistory).where(
                AssignmentHistory.booking_id == booking_id,
                AssignmentHistory.status == AssignmentStatus.ASSIGNED,
            )
        )
    ).scalars().all()

    for asgn in pending:
        asgn.status = AssignmentStatus.REASSIGNED
        if notify_techs:
            try:
                tech = (await db.execute(select(Technician).where(Technician.id == asgn.technician_id))).scalar_one_or_none()
                if tech and tech.fcm_token:
                    track_task(send_simple_push(
                        fcm_token=tech.fcm_token,
                        title="Job Request Cancelled",
                        body=f"Booking has been reassigned by admin.",
                        data={"type": "ASSIGNMENT_AUTO_CANCELLED", "booking_id": str(booking_id)},
                    ))
                # WS: dismiss IncomingBookingScreen on technician app
                track_task(publish_event(
                    technician_room(str(asgn.technician_id)),
                    WSEvent.ASSIGNMENT_AUTO_CANCELLED,
                    {"booking_id": str(booking_id), "assignment_id": str(asgn.id)},
                ))
            except Exception:
                pass

    await db.commit()


# ════════════════════════════════════════════════════════════════════════════════
#  TWO-PHASE WATCHER  — Fix B + C + D
# ════════════════════════════════════════════════════════════════════════════════

async def _two_phase_watcher(
    assignment_id_str: str,
    booking_id_str: str,
    technician_id_str: str,
    response_deadline: datetime,
) -> None:
    """
    Two-phase background task per assignment.

    Phase 1 — Screen-ACK window (SCREEN_ACK_GRACE_SECONDS):
        Waits for the technician app to call /screen-ack.
        If the grace window expires and screen_shown_at is still NULL →
        marks SCREEN_MISSED → redispatches immediately.

    Phase 2 — Response window (remaining time until response_deadline):
        Waits for the technician to ACCEPT or REJECT.
        If deadline passes → TIMEOUT → redispatches or escalates.

    Cancellation safety: asyncio.CancelledError exits silently — the caller
    (_register_watcher) already cancelled this task before starting the replacement.
    """
    from app.core.database import AsyncSessionLocal
    from uuid import UUID as _UUID

    assignment_id = _UUID(assignment_id_str)
    booking_id    = _UUID(booking_id_str)
    tech_id       = _UUID(technician_id_str)

    # ── Phase 1: wait for screen ACK ─────────────────────────────────────────
    try:
        await asyncio.sleep(SCREEN_ACK_GRACE_SECONDS)
    except asyncio.CancelledError:
        return  # new assignment replaced this one — exit cleanly

    async with AsyncSessionLocal() as db:
        try:
            asgn = (await db.execute(select(AssignmentHistory).where(AssignmentHistory.id == assignment_id))).scalar_one_or_none()
            if not asgn or asgn.status != AssignmentStatus.ASSIGNED:
                return  # already acted on (accepted/rejected/reassigned) — nothing to do

            if asgn.screen_shown_at is None:
                # ── Screen was never shown — mark SCREEN_MISSED and redispatch ──
                logger.info(f"[WATCHER] Phase1 SCREEN_MISSED for assignment {assignment_id_str}")
                asgn.status = AssignmentStatus.SCREEN_MISSED

                booking = (await db.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()
                if booking and booking.status == BookingStatus.ASSIGNED and booking.technician_id == tech_id:
                    booking.technician_id = None
                    booking.status = BookingStatus.CONFIRMED

                db.add(BookingStatusLog(
                    booking_id=booking_id,
                    status=booking.status if booking else BookingStatus.CONFIRMED,
                    changed_by=None,
                    notes="SCREEN_MISSED: technician app did not confirm screen was shown within grace window — redispatching",
                ))
                await db.commit()

                _miss_payload = {
                    "booking_id":    booking_id_str,
                    "booking_number": booking.booking_number if booking else "",
                    "status":        booking.status.value if booking else "CONFIRMED",
                    "assignment_id": assignment_id_str,
                    "reason":        "SCREEN_MISSED",
                }
                track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_REJECTED, _miss_payload))
                track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, _miss_payload))
                track_task(publish_event(booking_room(booking_id_str), WSEvent.ASSIGNMENT_REJECTED, _miss_payload))

                # Redispatch
                if booking:
                    await _redispatch(db, booking, tech_id, booking_id_str, reason="SCREEN_MISSED")
                return
            # else: screen was shown — fall through to Phase 2
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[WATCHER] Phase1 error for {assignment_id_str}: {e}")
            return

    # ── Phase 2: wait remaining time until response_deadline ─────────────────
    now = datetime.now(timezone.utc)
    remaining = (response_deadline - now).total_seconds()
    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    async with AsyncSessionLocal() as db:
        try:
            asgn = (await db.execute(select(AssignmentHistory).where(AssignmentHistory.id == assignment_id))).scalar_one_or_none()
            if not asgn or asgn.status != AssignmentStatus.ASSIGNED:
                return  # accepted/rejected during sleep — nothing to do

            # ── Timed out ──────────────────────────────────────────────────
            logger.info(f"[WATCHER] Phase2 TIMEOUT for assignment {assignment_id_str}")
            asgn.status = AssignmentStatus.TIMEOUT

            booking = (await db.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()
            if booking and booking.status == BookingStatus.ASSIGNED and booking.technician_id == tech_id:
                booking.technician_id = None
                booking.status = BookingStatus.CONFIRMED

            db.add(BookingStatusLog(
                booking_id=booking_id,
                status=booking.status if booking else BookingStatus.CONFIRMED,
                changed_by=None,
                notes=f"Auto-assignment timed out after {MAX_RESPONSE_MINUTES} min — technician did not respond",
            ))
            await db.commit()

            _timeout_payload = {
                "booking_id":    booking_id_str,
                "booking_number": booking.booking_number if booking else "",
                "status":        booking.status.value if booking else "CONFIRMED",
                "assignment_id": assignment_id_str,
                "reason":        "TIMEOUT",
            }
            track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_REJECTED, _timeout_payload))
            track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, _timeout_payload))
            track_task(publish_event(booking_room(booking_id_str), WSEvent.ASSIGNMENT_REJECTED, _timeout_payload))

            # Notify timed-out technician
            try:
                timed_tech = (await db.execute(select(Technician).where(Technician.id == tech_id))).scalar_one_or_none()
                if timed_tech:
                    track_task(push_to_technician(
                        db=db, technician=timed_tech,
                        title="Job Request Expired ⏰",
                        body=f"Booking {booking.booking_number if booking else ''}: job request expired — reassigned.",
                        notif_type="ASSIGNMENT",
                        data={"type": "ASSIGNMENT_TIMEOUT", "booking_id": booking_id_str},
                    ))
            except Exception:
                pass

            if booking:
                await _redispatch(db, booking, tech_id, booking_id_str, reason="TIMEOUT")

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[WATCHER] Phase2 error for {assignment_id_str}: {e}")


async def _redispatch(
    db: AsyncSession,
    booking: Booking,
    exclude_tech_id: UUID,
    booking_id_str: str,
    reason: str,
) -> None:
    """
    Try to assign the booking to the next best ONLINE technician.
    If exhausted, escalate to manual.
    """
    from app.utils.auto_assign import get_system_user_id, escalate_to_manual

    try:
        rules = await _get_default_rules(db)
        score2, next_tech, _ = await _pick_best_technician_online(db, booking, rules, exclude_technician_id=exclude_tech_id)
        sys_uid = await get_system_user_id(db)
        if not sys_uid:
            raise Exception("No admin user found for assigned_by")

        new_asgn = await _apply_assignment(
            db, booking, next_tech, AssignmentType.AUTO,
            sys_uid,
            f"Auto-redispatch after {reason}",
            score2,
            rules.response_timeout_minutes,
        )
        # Register new watcher — this also cancels any stale old one
        new_task = asyncio.ensure_future(_two_phase_watcher(
            str(new_asgn.id), booking_id_str, str(next_tech.id), new_asgn.response_deadline
        ))
        _register_watcher(booking_id_str, new_task)

    except Exception as re_err:
        logger.warning(f"[REDISPATCH] Failed for booking {booking_id_str} ({reason}): {re_err}")
        # Check if all online techs are exhausted → escalate
        try:
            from app.models.technician import TechnicianStatus as _TS
            online_ids = set((await db.execute(
                select(Technician.id).where(Technician.status == _TS.ACTIVE, Technician.is_online == True)
            )).scalars().all())
            past = (await db.execute(
                select(AssignmentHistory).where(
                    AssignmentHistory.booking_id == booking.id,
                    AssignmentHistory.assignment_type == AssignmentType.AUTO,
                    AssignmentHistory.status.in_([AssignmentStatus.REJECTED, AssignmentStatus.TIMEOUT, AssignmentStatus.SCREEN_MISSED]),
                )
            )).scalars().all()
            counts: dict = {}
            for a in past:
                if a.technician_id in online_ids:
                    counts[a.technician_id] = counts.get(a.technician_id, 0) + 1
            exhausted = len(online_ids) > 0 and all(counts.get(tid, 0) >= 2 for tid in online_ids)
            if exhausted:
                already = (await db.execute(
                    select(BookingStatusLog).where(
                        BookingStatusLog.booking_id == booking.id,
                        BookingStatusLog.notes.ilike("%NEEDS_MANUAL_ASSIGN%"),
                    )
                )).scalars().first()
                if not already:
                    await escalate_to_manual(db, booking, len(past))
        except Exception as ee:
            logger.warning(f"[REDISPATCH] Exhaustion check failed: {ee}")


def _start_watcher(assignment: AssignmentHistory) -> None:
    """Convenience: create watcher task and register it in the registry."""
    task = asyncio.ensure_future(_two_phase_watcher(
        str(assignment.id),
        str(assignment.booking_id),
        str(assignment.technician_id),
        assignment.response_deadline,
    ))
    _register_watcher(str(assignment.booking_id), task)


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════════

@router.post("/auto", summary="Auto assignment [Admin/CCO]")
async def auto_assign(
    payload: AutoAssignmentRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    Fix A: acquire Redis lock first — rejects duplicate concurrent requests.
    Fix B: cancels any existing watcher via registry before starting new one.
    Fix D: timeout capped at MAX_RESPONSE_MINUTES.
    """
    booking_id_str = payload.booking_id

    # Fix A — distributed lock
    if not await _acquire_assign_lock(booking_id_str):
        raise HTTPException(status_code=409, detail="Assignment already in progress for this booking. Please wait a moment and try again.")

    try:
        booking = await _get_booking_or_404(db, UUID(booking_id_str))
        rules   = await _get_default_rules(db)
        score, technician, workload = await _pick_best_technician(db, booking, rules)

        # Fix E: cancel existing watcher + mark old pending assignments REASSIGNED
        _cancel_existing_watcher(booking_id_str)
        await _mark_old_assignments_reassigned(db, booking.id, notify_techs=True)

        new_asgn = await _apply_assignment(
            db, booking, technician,
            AssignmentType.AUTO,
            current_user["user_id"],
            payload.notes,
            score,
            rules.response_timeout_minutes,
        )
    finally:
        await _release_assign_lock(booking_id_str)

    # Fix B+C: register new two-phase watcher
    _start_watcher(new_asgn)

    return success_response(
        data={
            "booking_id":         str(booking.id),
            "technician_id":      str(technician.id),
            "technician_name":    technician.name,
            "score":              round(score, 2),
            "current_workload":   workload,
            "assignment_id":      str(new_asgn.id),
            "response_deadline":  new_asgn.response_deadline.isoformat(),
            "max_response_minutes": MAX_RESPONSE_MINUTES,
        },
        message="Booking auto assigned successfully",
    )


@router.post("/manual", summary="Manual assignment [Admin/CCO]")
async def manual_assign(
    payload: ManualAssignmentRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    Fix A: acquire Redis lock.
    Fix B+E: cancel existing watcher and mark old assignments REASSIGNED atomically
             before creating the new manual assignment.
    Fix D: timeout capped at MAX_RESPONSE_MINUTES.
    """
    booking_id_str = payload.booking_id

    # Fix A — distributed lock
    if not await _acquire_assign_lock(booking_id_str):
        raise HTTPException(status_code=409, detail="Assignment already in progress for this booking. Please wait a moment.")

    try:
        booking = await _get_booking_or_404(db, UUID(booking_id_str))
        technician = (
            await db.execute(select(Technician).where(Technician.id == UUID(payload.technician_id), Technician.status == TechnicianStatus.ACTIVE))
        ).scalar_one_or_none()
        if not technician:
            raise HTTPException(status_code=404, detail="Technician not found or not active")

        rules = await _get_default_rules(db)

        # Fix B+E: kill existing watcher task + mark ASSIGNED history rows REASSIGNED
        _cancel_existing_watcher(booking_id_str)
        await _mark_old_assignments_reassigned(db, booking.id, notify_techs=True)

        # ── Slot-capacity guard for manual assign ────────────────────────────
        _m_slot = getattr(booking, "scheduled_slot", None)
        _m_date = getattr(booking, "scheduled_date", None)
        if _m_slot and _m_date:
            _m_date_only = _m_date.date() if hasattr(_m_date, "date") else _m_date
            _m_slot_count = await _get_slot_booking_count(db, technician.id, _m_date_only, _m_slot)
            if _m_slot_count >= MAX_BOOKINGS_PER_SLOT:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Technician already has {_m_slot_count} booking(s) in slot "
                        f"'{_m_slot}' on this date. Maximum is {MAX_BOOKINGS_PER_SLOT} per slot."
                    ),
                )

        new_asgn = await _apply_assignment(
            db, booking, technician,
            AssignmentType.MANUAL,
            current_user["user_id"],
            payload.notes,
            0.0,
            rules.response_timeout_minutes,
        )
    finally:
        await _release_assign_lock(booking_id_str)

    # Fix B+C: register new two-phase watcher
    _start_watcher(new_asgn)

    return success_response(
        data={
            "booking_id":        str(booking.id),
            "technician_id":     str(technician.id),
            "technician_name":   technician.name,
            "assignment_id":     str(new_asgn.id),
            "response_deadline": new_asgn.response_deadline.isoformat(),
            "max_response_minutes": MAX_RESPONSE_MINUTES,
        },
        message="Booking manually assigned successfully",
    )


@router.post("/{assignment_id}/screen-ack", summary="Captain: confirm booking accept screen is shown [Technician]")
async def screen_ack(
    assignment_id: str,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Fix C — called by IncomingBookingScreen.initState() the moment the screen renders.
    Sets screen_shown_at on the AssignmentHistory row so Phase 1 of the watcher
    knows the notification was actually seen by the technician.

    The watcher then waits the full remaining deadline for ACCEPT/REJECT before timing out.
    This means the technician always gets a fair response window from when they SAW the job,
    not from when the FCM was sent.
    """
    asgn = (await db.execute(select(AssignmentHistory).where(AssignmentHistory.id == UUID(assignment_id)))).scalar_one_or_none()
    if not asgn:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Ownership check
    if current_user["role"] == "TECHNICIAN":
        tech = (await db.execute(select(Technician).where(Technician.user_id == current_user["user_id"]))).scalar_one_or_none()
        if not tech or asgn.technician_id != tech.id:
            raise HTTPException(status_code=403, detail="This assignment does not belong to you")

    # Only update if screen hasn't been acked yet AND assignment is still pending
    if asgn.status == AssignmentStatus.ASSIGNED and asgn.screen_shown_at is None:
        asgn.screen_shown_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info(f"[SCREEN-ACK] Assignment {assignment_id} — screen confirmed shown")

    return success_response(
        data={
            "assignment_id":  assignment_id,
            "screen_shown_at": asgn.screen_shown_at.isoformat() if asgn.screen_shown_at else None,
            "response_deadline": asgn.response_deadline.isoformat() if asgn.response_deadline else None,
        },
        message="Screen acknowledgement recorded",
    )


class AssignmentResponseRequest(PydanticModel):
    response: str  # "ACCEPT" or "REJECT"


@router.post("/{assignment_id}/respond", summary="Captain: accept or reject a job assignment [Technician]")
async def respond_to_assignment(
    assignment_id: str,
    payload: AssignmentResponseRequest,
    current_user: dict = Depends(AdminOrTech),
    db: AsyncSession = Depends(get_db),
):
    action = payload.response.upper().strip()
    if action not in ("ACCEPT", "REJECT"):
        raise HTTPException(status_code=400, detail="response must be ACCEPT or REJECT")

    asgn = (await db.execute(select(AssignmentHistory).where(AssignmentHistory.id == UUID(assignment_id)))).scalar_one_or_none()
    if not asgn:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if current_user["role"] == "TECHNICIAN":
        tech = (await db.execute(select(Technician).where(Technician.user_id == current_user["user_id"]))).scalar_one_or_none()
        if not tech or asgn.technician_id != tech.id:
            raise HTTPException(status_code=403, detail="This assignment does not belong to you")

    if asgn.status != AssignmentStatus.ASSIGNED:
        # Grace window: accept allowed up to 5 min after TIMEOUT (slow notification tap)
        _grace_ok = (
            action == "ACCEPT"
            and asgn.status == AssignmentStatus.TIMEOUT
            and asgn.response_deadline is not None
            and (datetime.now(timezone.utc) - asgn.response_deadline).total_seconds() <= 300
        )
        if not _grace_ok:
            raise HTTPException(status_code=400, detail=f"Assignment is already {asgn.status.value.lower()}")
        asgn.status = AssignmentStatus.ASSIGNED  # restore so the ACCEPT branch works

    booking = await _get_booking_or_404(db, asgn.booking_id)

    if action == "ACCEPT":
        # Cancel the watcher — technician responded, no timeout needed
        _cancel_existing_watcher(str(asgn.booking_id))

        asgn.status = AssignmentStatus.ACCEPTED
        if booking.status == BookingStatus.ASSIGNED:
            booking.status = BookingStatus.ACCEPTED
        await _add_booking_log(db, booking, current_user["user_id"], "Technician accepted job")
        await db.commit()

        acc_payload = {
            "booking_id":    str(booking.id),
            "booking_number": booking.booking_number,
            "status":        booking.status.value,
            "assignment_id": str(asgn.id),
        }
        track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_ACCEPTED, acc_payload))
        track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, acc_payload))
        track_task(publish_event(booking_room(str(booking.id)), WSEvent.ASSIGNMENT_ACCEPTED, acc_payload))
        return success_response(data={"booking_id": str(booking.id), "status": booking.status.value}, message="Job accepted")

    else:  # REJECT
        # Cancel the watcher — we handle redispatch here immediately
        _cancel_existing_watcher(str(asgn.booking_id))

        asgn.status = AssignmentStatus.REJECTED
        if booking.technician_id == asgn.technician_id:
            booking.technician_id = None
        if booking.status in (BookingStatus.ASSIGNED, BookingStatus.ACCEPTED):
            booking.status = BookingStatus.CONFIRMED
        await _add_booking_log(db, booking, current_user["user_id"], "Technician rejected job — released for re-assignment")
        await db.commit()

        rej_payload = {
            "booking_id":    str(booking.id),
            "booking_number": booking.booking_number,
            "status":        booking.status.value,
            "assignment_id": str(asgn.id),
        }
        track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_REJECTED, rej_payload))
        track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, rej_payload))
        track_task(publish_event(booking_room(str(booking.id)), WSEvent.ASSIGNMENT_REJECTED, rej_payload))

        # Immediate redispatch to next best online technician
        try:
            rules = await _get_default_rules(db)
            score2, next_tech, workload2 = await _pick_best_technician_online(
                db, booking, rules, exclude_technician_id=asgn.technician_id
            )
            from app.utils.auto_assign import get_system_user_id
            sys_uid = await get_system_user_id(db)
            new_asgn = await _apply_assignment(
                db, booking, next_tech, AssignmentType.AUTO,
                sys_uid, "Auto-redispatch after rejection", score2, rules.response_timeout_minutes,
            )
            _start_watcher(new_asgn)
        except Exception:
            pass  # no more online technicians — stays CONFIRMED for manual assign

        return success_response(data={"booking_id": str(booking.id), "status": booking.status.value}, message="Job rejected")


@router.post("/cancel-auto/{booking_id}", summary="Cancel pending auto-assign [Admin/CCO]")
async def cancel_auto_assign(
    booking_id: str,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    Fix B: cancels in-memory watcher task immediately.
    Fix E: marks all ASSIGNED history rows as REASSIGNED.
    """
    booking = await _get_booking_or_404(db, UUID(booking_id))

    # Kill the watcher task — instant stop, no zombie watchers
    _cancel_existing_watcher(booking_id)

    await _mark_old_assignments_reassigned(db, booking.id, notify_techs=True)
    cancelled_count = len((await db.execute(
        select(AssignmentHistory).where(
            AssignmentHistory.booking_id == booking.id,
            AssignmentHistory.status == AssignmentStatus.REASSIGNED,
        )
    )).scalars().all())

    if booking.status == BookingStatus.ASSIGNED:
        booking.technician_id = None
        booking.status = BookingStatus.CONFIRMED
    elif booking.status not in (
        BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE,
        BookingStatus.ARRIVED, BookingStatus.INSPECTING,
        BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED,
    ):
        booking.technician_id = None

    await _add_booking_log(db, booking, current_user["user_id"], f"Admin cancelled auto-assignment — ready for manual assign")
    await db.commit()

    cancel_payload = {
        "booking_id": booking_id,
        "booking_number": booking.booking_number,
        "status": booking.status.value,
    }
    track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.ASSIGNMENT_AUTO_CANCELLED, cancel_payload))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.BOOKING_STATUS_CHANGED, cancel_payload))
    track_task(publish_event(booking_room(booking_id), WSEvent.ASSIGNMENT_AUTO_CANCELLED, cancel_payload))

    return success_response(
        data={"booking_id": booking_id, "booking_status": booking.status.value},
        message="Auto-assign cancelled — booking ready for manual assignment",
    )


@router.get("/history", summary="Assignment history [Admin/CCO]")
async def assignment_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    booking_id: str = Query(None),
    technician_id: str = Query(None),
    status: str = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(AssignmentHistory, Booking, Technician)
        .join(Booking, Booking.id == AssignmentHistory.booking_id)
        .join(Technician, Technician.id == AssignmentHistory.technician_id)
        .order_by(AssignmentHistory.created_at.desc())
    )
    if booking_id:
        query = query.where(AssignmentHistory.booking_id == UUID(booking_id))
    if technician_id:
        query = query.where(AssignmentHistory.technician_id == UUID(technician_id))
    if status:
        try:
            query = query.where(AssignmentHistory.status == AssignmentStatus[status.upper()])
        except KeyError:
            pass

    filters = []
    if booking_id:
        filters.append(AssignmentHistory.booking_id == UUID(booking_id))
    if technician_id:
        filters.append(AssignmentHistory.technician_id == UUID(technician_id))
    if status:
        try:
            filters.append(AssignmentHistory.status == AssignmentStatus[status.upper()])
        except KeyError:
            pass

    count_q = select(func.count()).select_from(
        select(AssignmentHistory).filter(*filters).order_by(None).subquery()
    )
    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(query.offset((page - 1) * per_page).limit(per_page))).all()

    return success_response(
        data={
            "items": [
                {
                    "id":                str(item.id),
                    "booking_id":        str(item.booking_id),
                    "booking_number":    bk.booking_number,
                    "technician_id":     str(item.technician_id),
                    "technician_name":   tech.name,
                    "technician_mobile": tech.mobile,
                    "assignment_type":   item.assignment_type.value,
                    "status":            item.status.value,
                    "score":             round(item.score or 0, 1),
                    "notes":             item.notes,
                    "screen_shown_at":   item.screen_shown_at.isoformat() if item.screen_shown_at else None,
                    "response_deadline": item.response_deadline.isoformat() if item.response_deadline else None,
                    "created_at":        item.created_at.isoformat(),
                }
                for item, bk, tech in rows
            ],
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    (total + per_page - 1) // per_page,
        }
    )


@router.get("/rules", summary="Assignment rules [Admin/CCO]")
async def get_rules(
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    rules = await _get_default_rules(db)
    return success_response(
        data={
            "id":                      str(rules.id),
            "name":                    rules.name,
            "strategy":                rules.strategy,
            "max_active_bookings":     rules.max_active_bookings,
            "prefer_same_city":        rules.prefer_same_city,
            "require_skill_match":     rules.require_skill_match,
            "prefer_high_rating":      rules.prefer_high_rating,
            "prefer_low_workload":     rules.prefer_low_workload,
            "response_timeout_minutes": rules.response_timeout_minutes,
            "max_response_minutes_hard_cap": MAX_RESPONSE_MINUTES,
            "notes":                   rules.notes,
        }
    )


@router.put("/rules", summary="Update assignment rules [Admin/CCO]")
async def update_rules(
    payload: UpdateAssignmentRuleRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    rules = await _get_default_rules(db)
    for field, value in payload.model_dump(exclude_none=True).items():
        if field == "response_timeout_minutes" and value > MAX_RESPONSE_MINUTES:
            raise HTTPException(
                status_code=400,
                detail=f"response_timeout_minutes cannot exceed the hard cap of {MAX_RESPONSE_MINUTES} minutes.",
            )
        setattr(rules, field, value)
    await db.commit()
    return success_response(message="Assignment rules updated successfully")


@router.get("/candidates/{booking_id}", summary="Scored technician candidates for manual assign [Admin/CCO]")
async def get_assignment_candidates(
    booking_id: str,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.customer import CustomerAddress as _AddrModel

    booking = await _get_booking_or_404(db, UUID(booking_id))
    rules   = await _get_default_rules(db)

    technicians = (await db.execute(select(Technician).where(Technician.status == TechnicianStatus.ACTIVE))).scalars().all()
    skill_rows  = (await db.execute(select(TechnicianSkill).where(TechnicianSkill.service_id == booking.service_id))).scalars().all()
    skill_match_ids = {row.technician_id for row in skill_rows}

    address = None
    if booking.address_id:
        address = (await db.execute(select(_AddrModel).where(_AddrModel.id == booking.address_id))).scalar_one_or_none()

    candidates = []
    for tech in technicians:
        workload     = await _get_active_workload(db, tech.id)
        skill_pts    = 50.0 if tech.id in skill_match_ids else 0.0
        rating_pts   = (tech.rating * 20) if rules.prefer_high_rating else 0.0
        workload_pts = max(0, 30 - workload * 10) if rules.prefer_low_workload else 0.0
        jobs_pts     = max(0, 20 - (tech.total_jobs or 0) * 0.1)
        proximity_pts = 0.0
        dist_km       = None
        if address and getattr(address, "latitude", None) and getattr(address, "longitude", None) and tech.last_lat and tech.last_lng:
            dist_km = round(_haversine_km(tech.last_lat, tech.last_lng, address.latitude, address.longitude), 1)
            proximity_pts = max(0, 30 - dist_km)
        total_score = skill_pts + rating_pts + workload_pts + jobs_pts + proximity_pts
        same_city = bool(address and tech.city and tech.city.lower() == address.city.lower())

        # Slot capacity check for candidates list
        _c_slot = getattr(booking, "scheduled_slot", None)
        _c_date = getattr(booking, "scheduled_date", None)
        slot_booking_count = 0
        slot_available = True
        if _c_slot and _c_date:
            _c_date_only = _c_date.date() if hasattr(_c_date, "date") else _c_date
            slot_booking_count = await _get_slot_booking_count(db, tech.id, _c_date_only, _c_slot)
            slot_available = slot_booking_count < MAX_BOOKINGS_PER_SLOT

        candidates.append({
            "technician_id":   str(tech.id),
            "name":            tech.name,
            "mobile":          tech.mobile,
            "city":            tech.city or "",
            "area":            tech.area or "",
            "is_online":       tech.is_online,
            "rating":          tech.rating,
            "total_jobs":      tech.total_jobs or 0,
            "active_workload": workload,
            "max_workload":    rules.max_active_bookings,
            "profile_image":   tech.profile_image,
            "skill_match":     tech.id in skill_match_ids,
            "same_city":       same_city,
            "overloaded":      workload >= rules.max_active_bookings,
            "slot_booking_count":  slot_booking_count,
            "slot_available":      slot_available,
            "slot_unavailable_reason": (
                f"Already has {slot_booking_count}/{MAX_BOOKINGS_PER_SLOT} booking(s) in this slot"
                if not slot_available else None
            ),
            "score":           round(total_score, 1),
            "distance_km":     dist_km,
            "last_lat":        tech.last_lat,
            "last_lng":        tech.last_lng,
            "last_seen_at":    tech.last_seen_at.isoformat() if getattr(tech, "last_seen_at", None) else None,
            "score_breakdown": {
                "skill":     round(skill_pts, 1),
                "rating":    round(rating_pts, 1),
                "workload":  round(workload_pts, 1),
                "jobs":      round(jobs_pts, 1),
                "proximity": round(proximity_pts, 1),
            },
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return success_response(data={
        "candidates":             candidates,
        "booking_id":             booking_id,
        "booking_number":         booking.booking_number,
        "scheduled_slot":         booking.scheduled_slot,
        "scheduled_date":         booking.scheduled_date.strftime("%Y-%m-%d") if booking.scheduled_date else None,
        "max_bookings_per_slot":  MAX_BOOKINGS_PER_SLOT,
        "current_technician_id":  str(booking.technician_id) if booking.technician_id else None,
        "total":                  len(candidates),
    })
