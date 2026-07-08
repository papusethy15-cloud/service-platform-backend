"""
WebSocket Connection Manager + Redis Pub/Sub Bridge
════════════════════════════════════════════════════

Architecture
────────────
  Any part of the app (FastAPI route, Celery task, assignment engine)
  publishes an event to a Redis channel.

  The WS manager subscribes to those Redis channels and fans the
  message out to every connected browser/app client that is subscribed
  to that room.

  This design works correctly across multiple Uvicorn worker processes:
  all workers share the same Redis pub/sub so every client receives
  every event regardless of which process owns their WebSocket.

Rooms / channels
────────────────
  booking:{booking_id}          — booking-level events
  admin:assignments             — all assignment events (admin dashboard)
  admin:bookings                — all booking status changes (admin dashboard)
  technician:{technician_id}    — events for a specific technician

Event envelope (JSON)
─────────────────────
  {
    "type":      "BOOKING_STATUS_CHANGED" | "ASSIGNMENT_CREATED" | ...
    "room":      "booking:abc-123"
    "payload":   { ... }
    "timestamp": "2026-01-01T10:00:00Z"
  }
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Set

import redis.asyncio as aioredis
from fastapi import WebSocket

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─── Event type constants ────────────────────────────────────────────────────
class WSEvent:
    # Assignment events
    ASSIGNMENT_CREATED        = "ASSIGNMENT_CREATED"
    ASSIGNMENT_ACCEPTED       = "ASSIGNMENT_ACCEPTED"
    ASSIGNMENT_REJECTED       = "ASSIGNMENT_REJECTED"
    ASSIGNMENT_AUTO_CANCELLED = "ASSIGNMENT_AUTO_CANCELLED"

    # Booking events
    BOOKING_STATUS_CHANGED    = "BOOKING_STATUS_CHANGED"
    BOOKING_TECHNICIAN_UPDATED = "BOOKING_TECHNICIAN_UPDATED"

    # Technician events
    TECHNICIAN_LOCATION_UPDATE  = "TECHNICIAN_LOCATION_UPDATE"
    TECHNICIAN_ONLINE_STATUS    = "TECHNICIAN_ONLINE_STATUS"
    TECHNICIAN_STATUS_CHANGED   = "TECHNICIAN_STATUS_CHANGED"   # auto-offline broadcasts

    # Dispatch / manual assign alerts
    BOOKING_NEEDS_MANUAL_ASSIGN = "BOOKING_NEEDS_MANUAL_ASSIGN"

    # Quotation events (real-time admin <-> technician sync)
    QUOTATION_CREATED = "QUOTATION_CREATED"
    QUOTATION_UPDATED = "QUOTATION_UPDATED"
    QUOTATION_DELETED = "QUOTATION_DELETED"

    # New booking from website
    BOOKING_CREATED           = "BOOKING_CREATED"

    # Payment / cash collection
    PAYMENT_COLLECTED         = "PAYMENT_COLLECTED"
    PAYMENT_DUE_REMINDER      = "PAYMENT_DUE_REMINDER"  # pay-later collection reminder sweep

    # Customer callback request
    CALLBACK_REQUEST          = "CALLBACK_REQUEST"

    # Quotation submitted by technician (needs admin approval)
    QUOTATION_SUBMITTED       = "QUOTATION_SUBMITTED"
    QUOTATION_APPROVED        = "QUOTATION_APPROVED"

    # Inspection submitted (by technician OR CCO on behalf)
    INSPECTION_SUBMITTED       = "INSPECTION_SUBMITTED"

    # Ping/pong
    PING = "PING"
    PONG = "PONG"


# ─── Room helpers ────────────────────────────────────────────────────────────
def booking_room(booking_id: str) -> str:
    return f"booking:{booking_id}"

def technician_room(technician_id: str) -> str:
    return f"technician:{technician_id}"

def customer_room(user_id: str) -> str:
    return f"customer:{user_id}"

ADMIN_ASSIGNMENTS_ROOM = "admin:assignments"
ADMIN_BOOKINGS_ROOM    = "admin:bookings"


# ─── ConnectionManager ───────────────────────────────────────────────────────
class ConnectionManager:
    """
    Manages all active WebSocket connections grouped by room.
    Each room maps to a set of WebSocket objects.
    """

    def __init__(self):
        # room → set of connected WebSockets
        self._rooms: Dict[str, Set[WebSocket]] = defaultdict(set)
        # websocket → set of rooms it belongs to (for cleanup on disconnect)
        self._ws_rooms: Dict[WebSocket, Set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, rooms: list[str]):
        await ws.accept()
        async with self._lock:
            for room in rooms:
                self._rooms[room].add(ws)
                self._ws_rooms[ws].add(room)
        logger.info(f"WS connected, rooms={rooms}, total_rooms={len(self._rooms)}")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            for room in self._ws_rooms.get(ws, set()):
                self._rooms[room].discard(ws)
                if not self._rooms[room]:
                    del self._rooms[room]
            self._ws_rooms.pop(ws, None)
        logger.info(f"WS disconnected, remaining_rooms={len(self._rooms)}")

    async def broadcast_to_room(self, room: str, message: dict):
        """Send message to every client subscribed to this room."""
        payload = json.dumps(message, default=str)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(room, set())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    def room_size(self, room: str) -> int:
        return len(self._rooms.get(room, set()))

    def total_connections(self) -> int:
        return sum(len(ws_set) for ws_set in self._rooms.values())


# ─── Singleton manager ───────────────────────────────────────────────────────
manager = ConnectionManager()


# ─── Redis Pub/Sub subscriber (runs as background task) ──────────────────────
_subscriber_task: asyncio.Task | None = None

async def _redis_subscriber():
    """
    Connects to Redis, subscribes to the master channel, and relays every
    published message to the appropriate WS room.
    Runs forever; auto-reconnects on connection loss.
    """
    channel_name = "palei:ws:events"
    while True:
        try:
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel_name)
            logger.info(f"[WS] Redis subscriber started on channel '{channel_name}'")
            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "message":
                    continue
                try:
                    event = json.loads(raw_msg["data"])
                    room  = event.get("room")
                    if room:
                        await manager.broadcast_to_room(room, event)
                except Exception as parse_err:
                    logger.warning(f"[WS] Bad event payload: {parse_err}")
        except asyncio.CancelledError:
            logger.info("[WS] Redis subscriber cancelled")
            break
        except Exception as conn_err:
            logger.warning(f"[WS] Redis subscriber error: {conn_err} — reconnecting in 3 s")
            await asyncio.sleep(3)


async def start_redis_subscriber():
    """Call once at app startup to launch the background listener."""
    global _subscriber_task
    if _subscriber_task is None or _subscriber_task.done():
        _subscriber_task = asyncio.create_task(_redis_subscriber())
        logger.info("[WS] Redis pub/sub subscriber task started")


async def stop_redis_subscriber():
    """Call at app shutdown."""
    global _subscriber_task
    if _subscriber_task and not _subscriber_task.done():
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
        _subscriber_task = None


# ─── Publish helper (called from routes / Celery bridge) ─────────────────────
async def publish_event(room: str, event_type: str, payload: dict):
    """
    Publish an event to ALL Uvicorn workers via Redis pub/sub.

    IMPORTANT — single delivery guarantee
    ──────────────────────────────────────
    We publish ONLY to Redis and let the subscriber relay it back.
    We do NOT also call broadcast_to_room() here, because the Redis
    subscriber runs in the same process (single-worker dev) and would
    deliver the message twice:
      1. direct broadcast_to_room()  ← first delivery
      2. Redis → subscriber → broadcast_to_room()  ← duplicate delivery

    In multi-worker production each worker's subscriber relays the
    message to its own connected sockets — exactly what we want.
    """
    event = {
        "type":      event_type,
        "room":      room,
        "payload":   payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Publish to Redis — the subscriber in EVERY worker (including this one)
    # will receive it and call broadcast_to_room() exactly once.
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.publish("palei:ws:events", json.dumps(event, default=str))
        await redis_client.aclose()
    except Exception as e:
        logger.warning(f"[WS] Redis publish failed — falling back to direct broadcast: {e}")
        # Fallback: if Redis is down, broadcast directly so the event isn't lost
        await manager.broadcast_to_room(room, event)


def publish_event_sync(room: str, event_type: str, payload: dict):
    """
    Synchronous version for Celery tasks (uses a fresh event loop or
    schedules on the running loop if available).
    """
    import redis as sync_redis
    event = {
        "type":      event_type,
        "room":      room,
        "payload":   payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish("palei:ws:events", json.dumps(event, default=str))
        r.close()
    except Exception as e:
        logger.warning(f"[WS] Celery Redis publish failed (non-critical): {e}")
