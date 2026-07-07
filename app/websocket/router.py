"""
WebSocket endpoints
═══════════════════

Endpoints
─────────
  WS /ws/admin/assignments
      Admin dashboard — receives all assignment & booking events.
      Requires JWT as query param: ?token=<access_token>

  WS /ws/booking/{booking_id}
      Track a single booking (admin, customer, or technician).
      Requires ?token=<access_token>

  WS /ws/technician/{technician_id}
      Technician app — receives job offers + booking updates.
      Requires ?token=<access_token>

Auth
────
  WebSocket connections cannot send HTTP headers after the handshake,
  so the JWT is passed as ?token= query parameter.
  The endpoint validates it and closes the connection with 4001 on failure.

Client protocol (JSON text frames)
───────────────────────────────────
  Client → Server:
    { "type": "PING" }
    { "type": "SUBSCRIBE", "rooms": ["booking:abc", "admin:assignments"] }

  Server → Client:
    { "type": "PONG", "room": null, "payload": {}, "timestamp": "..." }
    { "type": "CONNECTED", "room": null, "payload": { "rooms": [...], "connections": N } }
    { "type": "BOOKING_STATUS_CHANGED", "room": "booking:abc", "payload": {...}, "timestamp": "..." }
    ...etc
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError

from app.core.security import decode_token
from app.websocket.manager import (
    manager,
    WSEvent,
    booking_room,
    technician_room,
    ADMIN_ASSIGNMENTS_ROOM,
    ADMIN_BOOKINGS_ROOM,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── token validation helper ─────────────────────────────────────────────────
async def _validate_ws_token(ws: WebSocket, token: str | None) -> dict | None:
    """Validates JWT from query param. Closes connection and returns None on failure."""
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return None
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        role    = payload.get("role")
        if not user_id:
            await ws.close(code=4001, reason="Invalid token")
            return None
        return {"user_id": user_id, "role": role}
    except JWTError:
        await ws.close(code=4001, reason="Token expired or invalid")
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ws_loop(ws: WebSocket, rooms: list[str], user: dict):
    """
    Main message loop for any WS endpoint.
    Handles PING → PONG and SUBSCRIBE to additional rooms.
    Sends CONNECTED confirmation on startup.
    """
    try:
        # Confirm connection
        await ws.send_text(json.dumps({
            "type":      "CONNECTED",
            "room":      None,
            "payload":   {
                "rooms":       rooms,
                "connections": manager.total_connections(),
                "user_id":     user["user_id"],
                "role":        user["role"],
            },
            "timestamp": _now_iso(),
        }))

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == WSEvent.PING:
                await ws.send_text(json.dumps({
                    "type": WSEvent.PONG, "room": None, "payload": {}, "timestamp": _now_iso()
                }))

            elif msg_type == "SUBSCRIBE":
                # Allow client to subscribe to additional rooms at runtime
                extra_rooms = msg.get("rooms", [])
                if extra_rooms:
                    await manager.connect(ws, extra_rooms)
                    await ws.send_text(json.dumps({
                        "type":      "SUBSCRIBED",
                        "room":      None,
                        "payload":   {"rooms": extra_rooms},
                        "timestamp": _now_iso(),
                    }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[WS] loop error for user={user['user_id']}: {e}")
    finally:
        await manager.disconnect(ws)


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.websocket("/ws/admin/assignments")
async def ws_admin_assignments(
    ws: WebSocket,
    token: str | None = Query(default=None),
):
    """
    Admin / CCO dashboard WebSocket.
    Subscribes to:
      - admin:assignments  (all assignment events)
      - admin:bookings     (all booking status changes)
    """
    user = await _validate_ws_token(ws, token)
    if not user:
        return
    if user["role"] not in ("SUPER_ADMIN", "ADMIN", "CCO"):
        await ws.close(code=4003, reason="Forbidden")
        return

    rooms = [ADMIN_ASSIGNMENTS_ROOM, ADMIN_BOOKINGS_ROOM]
    await manager.connect(ws, rooms)
    logger.info(f"[WS] Admin connected user={user['user_id']} role={user['role']}")
    await _ws_loop(ws, rooms, user)


@router.websocket("/ws/booking/{booking_id}")
async def ws_booking(
    ws: WebSocket,
    booking_id: str,
    token: str | None = Query(default=None),
):
    """
    Single-booking tracking stream.
    Used by admin detail modal, customer app, or CCO to watch one booking in real time.
    """
    user = await _validate_ws_token(ws, token)
    if not user:
        return

    rooms = [booking_room(booking_id), ADMIN_BOOKINGS_ROOM]
    await manager.connect(ws, rooms)
    logger.info(f"[WS] Booking {booking_id} tracked by user={user['user_id']}")
    await _ws_loop(ws, rooms, user)


@router.websocket("/ws/technician/{technician_id}")
async def ws_technician(
    ws: WebSocket,
    technician_id: str,
    token: str | None = Query(default=None),
):
    """
    Technician-specific stream.
    Receives: ASSIGNMENT_CREATED, ASSIGNMENT_AUTO_CANCELLED, booking updates.
    """
    user = await _validate_ws_token(ws, token)
    if not user:
        return

    rooms = [technician_room(technician_id)]
    await manager.connect(ws, rooms)
    logger.info(f"[WS] Technician {technician_id} connected user={user['user_id']}")
    await _ws_loop(ws, rooms, user)
