"""
app/utils/fcm.py

Firebase Cloud Messaging helper for the Palei backend.
Sends data-only push notifications to technician devices via FCM HTTP v1 API.

Usage:
    from app.utils.fcm import send_booking_push, send_simple_push
    await send_booking_push(fcm_token, ...)

Key design:
- Firebase Admin SDK is initialised lazily from DB settings (group=firebase, key=firebase_sdk_json).
- If SDK JSON changes in the DB the app is re-initialised automatically (version tracking via hash).
- All blocking firebase_admin calls run in a thread-pool executor to avoid blocking the async loop.
"""

import json
import hashlib
import logging
import asyncio
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Module-level state ────────────────────────────────────────────────────────
_firebase_app       = None   # current firebase_admin.App instance
_firebase_creds_hash = None  # SHA-256 of the last SDK JSON used to init the app


async def _get_firebase_app():
    """
    Returns a live firebase_admin.App, re-initialising whenever the SDK JSON in
    system_settings has changed (hash mismatch) or when first called.
    Thread-safe for single-process uvicorn; for multi-process add a lock.
    """
    global _firebase_app, _firebase_creds_hash

    try:
        import firebase_admin
        from firebase_admin import credentials

        # Load credentials from DB settings
        from app.core.database import AsyncSessionLocal
        from app.models.system_setting import SystemSetting
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SystemSetting).where(SystemSetting.group == "firebase")
            )).scalars().all()

        settings_map = {row.key: row.value for row in rows}
        sdk_json = settings_map.get("firebase_sdk_json", "").strip()

        if not sdk_json:
            logger.warning("FCM: firebase_sdk_json not configured in system settings.")
            return None

        # Re-init only when creds actually changed
        sdk_hash = hashlib.sha256(sdk_json.encode()).hexdigest()
        if _firebase_app is not None and _firebase_creds_hash == sdk_hash:
            return _firebase_app  # fast path — already initialised with current creds

        # Delete stale app (if any) before re-initialising
        if _firebase_app is not None:
            try:
                firebase_admin.delete_app(_firebase_app)
            except Exception:
                pass
            _firebase_app = None

        cred_dict = json.loads(sdk_json)
        cred = credentials.Certificate(cred_dict)
        _firebase_app = firebase_admin.initialize_app(cred)
        _firebase_creds_hash = sdk_hash
        logger.info(f"FCM: Firebase Admin SDK initialised for project '{cred_dict.get('project_id', '?')}'")
        return _firebase_app

    except Exception as e:
        logger.error(f"FCM: Failed to initialise Firebase Admin SDK: {e}")
        _firebase_app = None
        _firebase_creds_hash = None
        return None


# Public alias so other modules (e.g. auth routes) can reuse the same lazily-initialised app
get_firebase_app = _get_firebase_app


async def send_booking_push(
    fcm_token: str,
    assignment_id: str,
    booking_id: str,
    booking_number: str,
    customer_name: str,
    address: str,
    service_name: Optional[str],
    scheduled_date: Optional[str],
    scheduled_time: Optional[str],
    response_deadline: Optional[datetime],
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> bool:
    """
    Send a full-screen incoming booking push notification to a technician.
    Returns True if sent successfully.
    """
    if not fcm_token:
        logger.warning(f"FCM: No FCM token for assignment {assignment_id}")
        return False

    try:
        app = await _get_firebase_app()
        if app is None:
            return False

        import firebase_admin.messaging as fcm_messaging

        deadline_str = (
            response_deadline.astimezone(timezone.utc).isoformat()
            if response_deadline else ""
        )

        message = fcm_messaging.Message(
            token=fcm_token,
            # Data-only — Flutter handles display via flutter_local_notifications
            # with fullScreenIntent so it works when app is killed.
            data={
                "type":              "NEW_BOOKING",
                "assignment_id":     str(assignment_id),
                "booking_id":        str(booking_id),
                "booking_number":    booking_number or "",
                "customer_name":     customer_name or "",
                "address":           address or "",
                "service_name":      service_name or "",
                "scheduled_date":    str(scheduled_date) if scheduled_date else "",
                "scheduled_time":    str(scheduled_time) if scheduled_time else "",
                "response_deadline": deadline_str,
                "latitude":          str(latitude) if latitude is not None else "",
                "longitude":         str(longitude) if longitude is not None else "",
            },
            android=fcm_messaging.AndroidConfig(
                priority="high",   # wakes device immediately (data-only msgs need this)
                ttl=300,           # 5 min — matches response_deadline
            ),
        )

        # firebase_admin.messaging.send is blocking — run in executor
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: fcm_messaging.send(message)
        )
        logger.info(f"FCM: Booking push sent OK → message_id={response}, assignment={assignment_id}")
        return True

    except Exception as e:
        logger.error(f"FCM: Failed to send booking push (assignment={assignment_id}): {e}")
        return False


async def send_simple_push(fcm_token: str, title: str, body: str, data: dict = None) -> bool:
    """Send a simple notification push (for online ack, status updates, etc.)."""
    if not fcm_token:
        return False
    try:
        app = await _get_firebase_app()
        if app is None:
            return False

        import firebase_admin.messaging as fcm_messaging

        message = fcm_messaging.Message(
            token=fcm_token,
            notification=fcm_messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            android=fcm_messaging.AndroidConfig(priority="normal"),
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: fcm_messaging.send(message))
        return True
    except Exception as e:
        logger.error(f"FCM: Failed to send simple push: {e}")
        return False
