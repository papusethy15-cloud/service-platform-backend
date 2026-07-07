"""
app/utils/notify.py

Central notification helper for Palei backend.
Saves a DB Notification record AND sends FCM push in one call.
Use this instead of calling send_simple_push + db.add(Notification) separately.

Usage:
    from app.utils.notify import push_to_technician
    await push_to_technician(
        db=db,
        technician=tech,          # Technician ORM object
        title="Quotation Approved",
        body="Admin approved your quotation for booking BK12345678.",
        notif_type="BOOKING",     # used by _notifColor in Flutter
        data={"type": "QUOTATION_APPROVED", "booking_id": str(booking.id)},
    )
"""

import asyncio
from app.core.background_tasks import track_task
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID

logger = logging.getLogger(__name__)


async def push_to_technician(
    db: AsyncSession,
    technician,                      # Technician ORM row
    title: str,
    body: str,
    notif_type: str = "SYSTEM",      # ASSIGNMENT | BOOKING | PAYMENT | LEAVE | SYSTEM
    data: Optional[dict] = None,
    channel: str = "PUSH",
) -> None:
    """
    1. Save a Notification row for the technician's user account.
    2. Fire FCM push with retry (WebSocket-first, FCM fallback).
    Both are fire-and-forget so they never block the HTTP response.
    """
    track_task(
        _save_and_push(db, technician, title, body, notif_type, data or {}, channel)
    )


async def _save_and_push(
    db: AsyncSession,
    technician,
    title: str,
    body: str,
    notif_type: str,
    data: dict,
    channel: str,
) -> None:
    """
    Runs as a background task (ensure_future).
    Opens its own DB session so the caller's session can close independently.
    """
    from app.core.database import AsyncSessionLocal
    from app.models.notification import Notification
    from app.models.technician import Technician
    from app.utils.fcm import send_simple_push

    try:
        async with AsyncSessionLocal() as new_db:
            # Re-fetch technician to get fresh fcm_token / user_id
            tech = (await new_db.execute(
                select(Technician).where(Technician.id == technician.id)
            )).scalar_one_or_none()
            if not tech:
                return

            # 1. Persist notification record
            notif_data = {**data, "notification_type": notif_type}
            notif = Notification(
                user_id=tech.user_id,
                title=title,
                body=body,
                channel=channel,
                data=notif_data,
                is_read=False,
            )
            new_db.add(notif)
            await new_db.commit()

            # 2. FCM push with simple retry (up to 3 attempts)
            if tech.fcm_token:
                push_data = {**data, "type": data.get("type", notif_type)}
                for attempt in range(1, 4):
                    sent = await send_simple_push(
                        fcm_token=tech.fcm_token,
                        title=title,
                        body=body,
                        data=push_data,
                    )
                    if sent:
                        logger.info(
                            f"[notify] FCM push OK (attempt {attempt}): {title} → technician {tech.id}"
                        )
                        break
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)  # 2s, 4s back-off
                    else:
                        logger.warning(
                            f"[notify] FCM push failed after 3 attempts: {title} → technician {tech.id}"
                        )

    except Exception as e:
        logger.error(f"[notify] push_to_technician error: {e}")


async def push_to_customer(
    db: AsyncSession,
    customer_id,                    # UUID of the Customer row
    title: str,
    body: str,
    notif_type: str = "BOOKING",    # BOOKING | PAYMENT | QUOTATION | SYSTEM
    data: Optional[dict] = None,
) -> None:
    """
    Save a Notification row for the customer's user account and fire an FCM push.
    Fire-and-forget — never blocks the HTTP response.
    """
    track_task(
        _save_and_push_customer(db, customer_id, title, body, notif_type, data or {})
    )


async def _save_and_push_customer(
    db: AsyncSession,
    customer_id,
    title: str,
    body: str,
    notif_type: str,
    data: dict,
) -> None:
    from app.core.database import AsyncSessionLocal
    from app.models.customer import Customer
    from app.models.user import User
    from app.models.notification import Notification
    from app.utils.fcm import send_simple_push

    try:
        async with AsyncSessionLocal() as new_db:
            customer = (await new_db.execute(
                select(Customer).where(Customer.id == customer_id)
            )).scalar_one_or_none()
            if not customer:
                return

            user = (await new_db.execute(
                select(User).where(User.id == customer.user_id)
            )).scalar_one_or_none()
            if not user:
                return

            # 1. Persist notification
            notif = Notification(
                user_id=user.id,
                title=title,
                body=body,
                channel="PUSH",
                data={**data, "notification_type": notif_type},
                is_read=False,
            )
            new_db.add(notif)
            await new_db.commit()

            # 2. FCM push (try customer-row token first, fall back to user-row token)
            token = customer.fcm_token or user.fcm_token
            if token:
                push_data = {**data, "type": data.get("type", notif_type)}
                for attempt in range(1, 4):
                    sent = await send_simple_push(
                        fcm_token=token,
                        title=title,
                        body=body,
                        data=push_data,
                    )
                    if sent:
                        logger.info(
                            f"[notify] Customer FCM push OK (attempt {attempt}): {title} → customer {customer_id}"
                        )
                        break
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.warning(
                            f"[notify] Customer FCM push failed after 3 attempts: {title} → customer {customer_id}"
                        )

    except Exception as e:
        logger.error(f"[notify] push_to_customer error: {e}")
