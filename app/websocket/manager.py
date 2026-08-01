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

  LOCAL DEV WITHOUT REDIS
  ───────────────────────
  If Redis is unavailable, the manager falls back to direct in-process
  broadcast. This means WebSocket events work normally in single-worker
  dev mode (uvicorn --reload). Multi-worker prod still requires Redis.

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

# ─── Redis availability flag ─────────────────────────────────────────────────
# Set to True once Redis connects successfully; False when connection fails.
# publish_event() reads this to decide whether to use Redis or direct broadcast.
_redis_available: bool = False


# ─── Event type constants ────────────────────────────────────────────────────
class WSEvent:
    PING                       = "PING"
    PONG                       = "PONG"
    CONNECTED                  = "CONNECTED"
    SUBSCRIBED                 = "SUBSCRIBED"
    BOOKING_STATUS_CHANGED     = "BOOKING_STATUS_CHANGED"
    BOOKING_CREATED            = "BOOKING_CREATED"
    BOOKING_NEEDS_MANUAL_ASSIGN= "BOOKING_NEEDS_MANUAL_ASSIGN"
    ASSIGNMENT_CREATED         = "ASSIGNMENT_CREATED"
    ASSIGNMENT_ACCEPTED        = "ASSIGNMENT_ACCEPTED"
    ASSIGNMENT_REJECTED        = "ASSIGNMENT_REJECTED"
    ASSIGNMENT_AUTO_CANCELLED  = "ASSIGNMENT_AUTO_CANCELLED"
    TECHNICIAN_STATUS_CHANGED  = "TECHNICIAN_STATUS_CHANGED"
    TECHNICIAN_LOCATION_UPDATE = "TECHNICIAN_LOCATION_UPDATE"
    QUOTATION_CREATED          = "QUOTATION_CREATED"
    QUOTATION_UPDATED          = "QUOTATION_UPDATED"
    QUOTATION_APPROVED         = "QUOTATION_APPROVED"
    QUOTATION_REJECTED         = "QUOTATION_REJECTED"
    QUOTATION_SUBMITTED        = "QUOTATION_SUBMITTED"
    QUOTATION_DELETED          = "QUOTATION_DELETED"
    INSPECTION_SUBMITTED       = "INSPECTION_SUBMITTED"
    PAYMENT_RECEIVED           = "PAYMENT_RECEIVED"
    PAYMENT_DUE_REMINDER       = "PAYMENT_DUE_REMINDER"
    WALLET_UPDATED             = "WALLET_UPDATED"
    WITHDRAWAL_UPDATED         = "WITHDRAWAL_UPDATED"
    NOTIFICATION               = "NOTIFICATION"


# ─── Room name helpers ───────────────────────────────────────────────────────
ADMIN_ASSIGNMENTS_ROOM = "admin:assignments"
ADMIN_BOOKINGS_ROOM    = "admin:bookings"

def booking_room(booking_id: str)     -> str: return f"booking:{booking_id}"
def technician_room(technician_id: str) -> str: return f"technician:{technician_id}"
def customer_room(user_id: str)       -> str: return f"customer:{user_id}"


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
        # NOTE: ws.accept() is called in _validate_ws_token (router.py) before
        # connect() is invoked. Do NOT call ws.accept() here — calling it twice
        # raises a Starlette error and closes the connection immediately.
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

# Exceptions that indicate a genuine Redis connection problem (not a read timeout).
# Used to decide whether to log "Redis unavailable" or silently retry.
_REDIS_REAL_ERRORS = (
    ConnectionRefusedError,       # Redis process not running / port closed
    ConnectionResetError,         # TCP connection dropped mid-session
    OSError,                      # General network / socket-level failure
)


async def _redis_subscriber():
    """
    Connects to Redis, subscribes to the master channel, and relays every
    published message to the appropriate WS room.
    Runs forever; auto-reconnects on connection loss.

    LOCAL DEV: If Redis is not available, logs a warning and waits 30s before
    retrying — does NOT spam the log every 3 seconds. WS connections still work
    via the direct-broadcast fallback in publish_event().

    FIX — socket_timeout removed from subscriber connection
    ────────────────────────────────────────────────────────
    The original code used socket_timeout=3 on the subscriber client.
    pubsub.listen() is a blocking call that waits indefinitely for the next
    Redis message; with socket_timeout=3 it raised a TimeoutError every ~3 s,
    which the except-block mis-classified as "Redis unavailable" and logged
    the warning repeatedly.

    The subscriber now uses socket_timeout=None (no read-timeout) so that
    pubsub.listen() can block as long as needed without raising a spurious
    TimeoutError.  socket_connect_timeout=3 is preserved so that an
    unreachable Redis host still fails fast at connection time.

    Improved exception handling
    ───────────────────────────
    Inside the pubsub.listen() loop we now distinguish between:
      • TimeoutError / asyncio.TimeoutError — expected if no message arrives
        for a while on a very quiet channel; we simply continue the loop.
      • Real connection errors (ConnectionRefusedError, OSError, etc.) —
        we break out of the loop so the outer while-True can reconnect.
      • Any other unexpected Exception — logged as a warning and we reconnect.
    This ensures "Redis unavailable" is only logged for genuine connectivity
    problems, not for normal Pub/Sub idle periods.
    """
    global _redis_available
    channel_name = "palei:ws:events"
    _first_attempt = True
    _consecutive_failures = 0

    while True:
        redis_client = None
        pubsub = None
        try:
            # ── CHANGE 1 ──────────────────────────────────────────────────────
            # Removed `socket_timeout=3` from the subscriber client.
            #
            # Reason: pubsub.listen() is designed to block indefinitely waiting
            # for the next message.  A non-None socket_timeout causes the
            # underlying socket to raise TimeoutError every few seconds even
            # when Redis is perfectly healthy, producing false "unavailable"
            # log spam.
            #
            # socket_connect_timeout=3 is kept so that an unreachable Redis
            # server still fails within 3 seconds at connection time.
            # ─────────────────────────────────────────────────────────────────
            redis_client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=3,   # fail fast if Redis is down at connect time
                socket_timeout=None,        # FIXED: no read-timeout on subscriber socket
            )

            # Test connectivity before entering the listen loop.
            await redis_client.ping()

            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel_name)

            if not _redis_available:
                logger.info(
                    f"[WS] Redis connected ✓ — subscriber started on '{channel_name}'"
                )
            _redis_available = True
            _consecutive_failures = 0
            _first_attempt = False

            # ── CHANGE 2 ──────────────────────────────────────────────────────
            # Replaced the bare `async for raw_msg in pubsub.listen()` with a
            # try/except inside the loop body so we can handle errors at the
            # message-receive level without tearing down the whole subscriber.
            #
            # • TimeoutError / asyncio.TimeoutError:
            #     Not a real error for a Pub/Sub listener — just means no
            #     message arrived recently.  We `continue` the inner loop
            #     without logging anything.
            #
            # • _REDIS_REAL_ERRORS (ConnectionRefusedError, OSError, etc.):
            #     Redis is genuinely gone.  Break so the outer while-True
            #     reconnects and logs "Redis unavailable".
            #
            # • Any other Exception:
            #     Log a warning and reconnect (safer than silently swallowing).
            # ─────────────────────────────────────────────────────────────────
            async for raw_msg in pubsub.listen():
                try:
                    if raw_msg["type"] != "message":
                        continue
                    event = json.loads(raw_msg["data"])
                    room = event.get("room")
                    if room:
                        await manager.broadcast_to_room(room, event)

                except (TimeoutError, asyncio.TimeoutError):
                    # Idle timeout on a quiet channel — not a real error.
                    # Continue waiting for the next message silently.
                    continue

                except _REDIS_REAL_ERRORS as real_err:
                    # Genuine connectivity loss — exit listen loop to reconnect.
                    logger.warning(
                        f"[WS] Redis connection lost during listen: {real_err}"
                    )
                    break

                except Exception as parse_err:
                    # Malformed JSON or unexpected payload — log and keep going.
                    logger.warning(f"[WS] Bad event payload: {parse_err}")

        except asyncio.CancelledError:
            logger.info("[WS] Redis subscriber cancelled")
            _redis_available = False
            break

        # ── CHANGE 3 ──────────────────────────────────────────────────────────
        # Improved outer exception handling: distinguish between a real Redis
        # error and anything else.  Only log "Redis unavailable" for errors
        # that actually indicate a connectivity problem.
        #
        # TimeoutError here means the initial ping() timed out — Redis really
        # is unreachable, so we DO log "unavailable" for that case.
        # ─────────────────────────────────────────────────────────────────────
        except (TimeoutError, asyncio.TimeoutError, *_REDIS_REAL_ERRORS) as conn_err:
            _redis_available = False
            _consecutive_failures += 1

            if _first_attempt or _consecutive_failures == 1:
                logger.warning(
                    f"[WS] Redis unavailable ({conn_err}). "
                    f"WebSocket events will use direct in-process broadcast (single-worker only). "
                    f"Start Redis to enable multi-worker pub/sub."
                )
                _first_attempt = False

            # Back off: 5s → 10s → 20s → 40s → 60s max, to avoid log spam.
            delay = min(5 * (2 ** min(_consecutive_failures - 1, 3)), 60)
            await asyncio.sleep(delay)

        except Exception as unexpected_err:
            # Catch-all for unexpected errors (auth failure, config error, etc.)
            _redis_available = False
            _consecutive_failures += 1

            if _first_attempt or _consecutive_failures == 1:
                logger.warning(
                    f"[WS] Redis unavailable — unexpected error ({type(unexpected_err).__name__}: {unexpected_err}). "
                    f"Falling back to direct broadcast."
                )
                _first_attempt = False

            delay = min(5 * (2 ** min(_consecutive_failures - 1, 3)), 60)
            await asyncio.sleep(delay)

        finally:
            # ── CHANGE 4 ──────────────────────────────────────────────────────
            # Always clean up pubsub and redis_client on exit from the try block
            # (whether due to error, break, or reconnect).  This prevents file-
            # descriptor leaks when the subscriber reconnects repeatedly.
            # ─────────────────────────────────────────────────────────────────
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe()
                    await pubsub.aclose()
                except Exception:
                    pass
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass


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

    FALLBACK BEHAVIOUR (Redis unavailable / local dev)
    ──────────────────────────────────────────────────
    When Redis is down, broadcasts directly to in-process WebSocket clients.
    This works perfectly in single-worker dev mode. In multi-worker production
    it only reaches clients on the current worker — start Redis for full fanout.
    """
    event = {
        "type":      event_type,
        "room":      room,
        "payload":   payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if _redis_available:
        # Production path: publish to Redis → subscriber relays to all workers
        try:
            redis_client = aioredis.from_url(
                settings.REDIS_URL, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
            await redis_client.publish("palei:ws:events", json.dumps(event, default=str))
            await redis_client.aclose()
            return
        except Exception as e:
            logger.warning(f"[WS] Redis publish failed — using direct broadcast: {e}")

    # Dev/fallback path: broadcast directly to in-process clients
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
        r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2)
        r.publish("palei:ws:events", json.dumps(event, default=str))
        r.close()
    except Exception as e:
        logger.warning(f"[WS] Celery Redis publish failed (non-critical): {e}")
