"""
app/utils/auto_assign.py
------------------------
Shared utilities for the auto-assignment system.
Kept separate from main.py and assignments.py to avoid circular imports.
"""
import asyncio
from app.core.background_tasks import track_task
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import BookingStatusLog
from app.models.user import User
from app.websocket.manager import (
    ADMIN_ASSIGNMENTS_ROOM,
    ADMIN_BOOKINGS_ROOM,
    WSEvent,
    publish_event,
)

_logger = logging.getLogger(__name__)


async def escalate_to_manual(db: AsyncSession, booking, attempt_count: int) -> None:
    """
    Called when auto-assign is exhausted (all online techs tried >= 2 times).

    Actions:
      1. Writes a NEEDS_MANUAL_ASSIGN BookingStatusLog (idempotent — caller
         must check for existing log before calling).
      2. Fires BOOKING_NEEDS_MANUAL_ASSIGN WebSocket event to both admin rooms.
      3. Sends FCM push to every ADMIN / SUPER_ADMIN / CCO user with an fcm_token.
    """
    from app.utils.fcm import send_simple_push  # local import to avoid top-level cycle

    # 1. Status log
    db.add(BookingStatusLog(
        booking_id=booking.id,
        status=booking.status,
        changed_by=None,
        notes=(
            f"NEEDS_MANUAL_ASSIGN: Auto-assign exhausted after {attempt_count} "
            f"attempts across all online technicians. Manual assignment required."
        ),
    ))
    await db.commit()

    # 2. WebSocket events
    _payload = {
        "booking_id":     str(booking.id),
        "booking_number": booking.booking_number,
        "status":         booking.status.value,
        "attempt_count":  attempt_count,
        "message": (
            f"Booking {booking.booking_number} could not be auto-assigned after "
            f"{attempt_count} attempts. Please assign manually."
        ),
        "action": "MANUAL_ASSIGN_REQUIRED",
    }
    track_task(publish_event(ADMIN_ASSIGNMENTS_ROOM, WSEvent.BOOKING_NEEDS_MANUAL_ASSIGN, _payload))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM,    WSEvent.BOOKING_NEEDS_MANUAL_ASSIGN, _payload))

    # 3. FCM push to all admin / CCO users
    try:
        admin_users = (await db.execute(
            select(User).where(
                User.role.in_(["SUPER_ADMIN", "ADMIN", "CCO"]),
                User.fcm_token.isnot(None),
                User.is_active == True,
            )
        )).scalars().all()

        for admin_user in admin_users:
            if admin_user.fcm_token:
                track_task(send_simple_push(
                    fcm_token=admin_user.fcm_token,
                    title="Manual Assignment Required",
                    body=f"Booking {booking.booking_number} needs manual technician assignment.",
                    data={
                        "type":           "BOOKING_NEEDS_MANUAL_ASSIGN",
                        "booking_id":     str(booking.id),
                        "booking_number": booking.booking_number,
                    },
                ))
    except Exception as _fe:
        _logger.warning(f"[AUTO-ASSIGN] FCM push error for manual escalation: {_fe}")

    _logger.warning(
        f"[AUTO-ASSIGN] Booking {booking.booking_number} escalated to MANUAL ASSIGN "
        f"after {attempt_count} auto-attempts"
    )


async def get_system_user_id(db: AsyncSession):
    """
    Returns the ID string of the first active SUPER_ADMIN or ADMIN user.
    Used as assigned_by in auto-assignment records (FK to users.id).
    Returns None if no admin user exists.
    """
    from app.models.user import UserRole
    user = (await db.execute(
        select(User).where(
            User.role.in_([UserRole.SUPER_ADMIN, UserRole.ADMIN]),
            User.is_active == True,
        ).limit(1)
    )).scalar_one_or_none()
    return str(user.id) if user else None
