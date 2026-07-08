from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.router import api_router
from app.websocket.router import router as ws_router
from app.websocket.manager import start_redis_subscriber, stop_redis_subscriber
from app.core.background_tasks import track_task, cancel_all_background_tasks


async def _auto_migrate():
    """
    Auto-apply ALL pending Alembic migrations on startup (upgrade to head).
    This handles both missing tables AND missing columns on existing tables,
    unlike create_all which only creates new tables.
    Runs in a thread-pool executor because Alembic's sync engine blocks the event loop.
    """
    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor

    def _run_alembic_upgrade():
        try:
            from alembic.config import Config
            from alembic import command as alembic_cmd
            from app.core.config import settings as _s
            import re as _re
            _safe_url = _re.sub(r':([^:@]+)@', ':***@', _s.DATABASE_URL)
            print(f"[INFO] Auto-migrate: connecting to {_safe_url}")

            # ── EARLY EXIT: skip alembic entirely if already at head ──────
            # This prevents the "Aborted!" in stderr on every restart.
            # alembic command.upgrade() invokes Click's CLI machinery which
            # calls sys.exit() on certain conditions → Click prints "Aborted!"
            # to stderr. We avoid calling it at all if the DB is already at head.
            # Uses subprocess psql (always available on VPS) for the version check.
            CURRENT_HEAD = "056"
            try:
                import subprocess as _sp
                _pg_url = _s.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
                _vcheck = _sp.run(
                    ["psql", _pg_url, "-t", "-A", "-c",
                     f"SELECT COUNT(*) FROM alembic_version WHERE version_num = '{CURRENT_HEAD}'"],
                    capture_output=True, text=True, timeout=10
                )
                _already_at_head = _vcheck.returncode == 0 and _vcheck.stdout.strip() == "1"
                if _already_at_head:
                    print("[OK] Auto-migrate: all Alembic migrations applied (head)")
                    return
            except Exception as _ve:
                print(f"[INFO] Auto-migrate: version check skipped ({_ve}) — running alembic")

            # Locate alembic.ini relative to the backend root
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ini_path = os.path.join(backend_dir, "alembic.ini")

            if not os.path.exists(ini_path):
                print(f"[WARN] Auto-migrate: alembic.ini not found at {ini_path}")
                return

            # Do NOT use cfg.set_main_option() — the DB password contains
            # %-encoded chars (%40, %23) which configparser misinterprets as
            # interpolation syntax → ValueError.
            # env.py reads settings.DATABASE_URL directly via asyncpg (no
            # psycopg2/sync driver needed) and handles legacy VPS baseline-
            # stamping automatically. Just point at alembic.ini and run.
            cfg = Config(ini_path)
            alembic_cmd.upgrade(cfg, "head")
            print("[OK] Auto-migrate: all Alembic migrations applied (head)")
        except Exception as e:
            print(f"[WARN] Auto-migrate failed: {e}")


    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        await loop.run_in_executor(pool, _run_alembic_upgrade)


async def _seed_admin():
    """Create the default super-admin user if it doesn't exist."""
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.user import User
        from app.core.security import hash_password
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.email == "admin@paleisolutions.com")
            )
            if not result.scalar_one_or_none():
                admin = User(
                    name="Super Admin",
                    email="admin@paleisolutions.com",
                    mobile="9999999999",
                    password_hash=hash_password("Srikanta@15"),
                    role="SUPER_ADMIN",
                    is_active=True,
                    is_verified=True,
                )
                session.add(admin)
                await session.commit()
                print("[OK] Admin seeded: admin@paleisolutions.com / Srikanta@15")
            else:
                print("[OK] Admin already exists")
    except Exception as e:
        print(f"[WARN] Admin seed skipped: {e}")



async def _auto_offline_stale_technicians():
    """
    Background task: runs every 2 minutes.
    Auto-offlines technicians whose last_seen_at > 10 minutes ago.
    This handles phone-off, app-kill, no internet scenarios.
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    while True:
        try:
            await asyncio.sleep(120)  # check every 2 minutes
            from app.core.database import AsyncSessionLocal
            from app.models.technician import Technician
            from sqlalchemy import select, update
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Technician).where(
                        Technician.is_online == True,
                        Technician.last_seen_at != None,
                        Technician.last_seen_at < cutoff,
                    )
                )
                stale = result.scalars().all()
                offline_ids = []
                for tech in stale:
                    tech.is_online    = False
                    tech.last_seen_at = None
                    offline_ids.append((str(tech.id), tech.name))
                    print(f"[AUTO-OFFLINE] Technician {tech.name} ({tech.id}) auto-offlined after 10min inactivity")
                if stale:
                    await db.commit()
                    # Broadcast WS event so admin dashboard updates in real time
                    try:
                        from app.websocket.manager import publish_event, WSEvent, ADMIN_ASSIGNMENTS_ROOM
                        for tech_id, tech_name in offline_ids:
                            import asyncio as _asyncio
                            track_task(publish_event(
                                ADMIN_ASSIGNMENTS_ROOM,
                                WSEvent.TECHNICIAN_STATUS_CHANGED,
                                {"technician_id": tech_id, "technician_name": tech_name,
                                 "is_online": False, "reason": "auto_offline_10min"},
                            ))
                    except Exception as _ws_err:
                        print(f"[AUTO-OFFLINE] WS publish error: {_ws_err}")
        except Exception as e:
            print(f"[AUTO-OFFLINE] Error: {e}")



async def _auto_retry_unassigned_bookings():
    """
    Background task: runs ONCE DAILY at 09:00 IST.

    For each CONFIRMED booking with no technician assigned:
      1. auto_assign_enabled must be ON
      2. At least one technician must be online
      3. Checks how many AUTO assignment ROUNDS have been attempted
         (a "round" = all online technicians have been tried once).
         We count unique REJECTED/TIMEOUT entries to determine attempts.
      4. If attempts < 2 rounds worth → try to assign next available online tech
         (skipping all techs who already rejected/timed-out this booking)
      5. If ALL online techs have already rejected/timed-out at least twice
         (i.e., 2 full rounds exhausted) → escalate to manual:
           - Publish WS BOOKING_NEEDS_MANUAL_ASSIGN event to admin
           - Send FCM push to all admin/CCO users
           - Mark booking with a status log noting manual assignment required
    """
    import asyncio
    import logging
    from datetime import datetime, timezone, timedelta

    _logger = logging.getLogger(__name__)

    while True:
        try:
            # ── Wait until 09:00 IST today (or tomorrow if already past) ──────
            # IST = UTC+5:30
            now_utc = datetime.now(timezone.utc)
            now_ist = now_utc + timedelta(hours=5, minutes=30)
            # Target: 09:00 IST today
            target_ist = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)
            if now_ist >= target_ist:
                # Already past 9 AM IST today → schedule for tomorrow 9 AM
                target_ist = target_ist + timedelta(days=1)
            sleep_seconds = (target_ist - now_ist).total_seconds()
            _logger.info(f"[AUTO-RETRY] Next run at 09:00 IST — sleeping {sleep_seconds/3600:.1f} h")
            await asyncio.sleep(sleep_seconds)

            from app.core.database import AsyncSessionLocal
            from app.models.system_setting import SystemSetting
            from app.models.booking import Booking, BookingStatus, BookingStatusLog
            from app.models.technician import Technician, TechnicianStatus
            from app.models.assignment import AssignmentHistory, AssignmentStatus, AssignmentType
            from app.models.user import User
            from sqlalchemy import select, func, and_
            from uuid import UUID as _UUID

            async with AsyncSessionLocal() as db:
                # ── 1. Check auto_assign_enabled ─────────────────────────────
                setting_row = (await db.execute(
                    select(SystemSetting).where(
                        SystemSetting.group == "dispatch",
                        SystemSetting.key == "auto_assign_enabled",
                    )
                )).scalar_one_or_none()
                enabled = (setting_row.value if setting_row else "true").strip().lower()
                if enabled != "true":
                    _logger.debug("[AUTO-RETRY] auto_assign_enabled=false — skipping")
                    continue

                # ── 2. Check at least one online technician ──────────────────
                online_count = (await db.execute(
                    select(func.count(Technician.id)).where(
                        Technician.status == TechnicianStatus.ACTIVE,
                        Technician.is_online == True,
                    )
                )).scalar_one()
                if online_count == 0:
                    _logger.debug("[AUTO-RETRY] No online technicians — skipping")
                    continue

                # ── 3. Find all CONFIRMED bookings with no technician assigned ──
                unassigned = (await db.execute(
                    select(Booking).where(
                        Booking.status == BookingStatus.CONFIRMED,
                        Booking.technician_id == None,
                        Booking.is_active == True,
                        Booking.service_id != None,
                    )
                )).scalars().all()

                if not unassigned:
                    _logger.debug("[AUTO-RETRY] No unassigned CONFIRMED bookings")
                    continue

                _logger.info(f"[AUTO-RETRY] Found {len(unassigned)} unassigned bookings to retry")

                from app.api.v1.routes.assignments import (
                    _get_default_rules, _apply_assignment, _timeout_watcher,
                    _pick_best_technician_online,
                )
                from app.utils.auto_assign import escalate_to_manual, get_system_user_id
                rules = await _get_default_rules(db)

                # Get a system user ID to use as assigned_by (FK to users.id)
                _system_user_id = await get_system_user_id(db)
                if not _system_user_id:
                    _logger.warning("[AUTO-RETRY] No admin user found for assigned_by — skipping cycle")
                    continue

                for booking in unassigned:
                    try:
                        bid = str(booking.id)

                        # ── Skip if already has an active pending assignment ────────
                        # Prevents race condition with _timeout_watcher
                        _active_asgn = (await db.execute(
                            select(AssignmentHistory).where(
                                AssignmentHistory.booking_id == booking.id,
                                AssignmentHistory.status == AssignmentStatus.ASSIGNED,
                            )
                        )).scalars().first()
                        if _active_asgn:
                            _logger.debug(f"[AUTO-RETRY] Booking {booking.booking_number} already has pending assignment — skipping")
                            continue

                        # ── Get all past AUTO assignment attempts for this booking ──
                        past_assignments = (await db.execute(
                            select(AssignmentHistory).where(
                                AssignmentHistory.booking_id == booking.id,
                                AssignmentHistory.assignment_type == AssignmentType.AUTO,
                                AssignmentHistory.status.in_([
                                    AssignmentStatus.REJECTED,
                                    AssignmentStatus.TIMEOUT,
                                ]),
                            )
                        )).scalars().all()

                        # IDs of techs who already rejected/timed-out this booking
                        rejected_tech_ids = {a.technician_id for a in past_assignments}

                        # ── 2-round exhaustion check ─────────────────────────
                        # A "round" = all CURRENT online techs have been tried.
                        # We compare unique rejected tech IDs vs current online pool.
                        # If every currently-online tech has been tried >= 2 times → escalate.
                        # This prevents escalating when only 1 tech is online and
                        # new techs come online later (they haven't been tried yet).
                        online_tech_ids = set((await db.execute(
                            select(Technician.id).where(
                                Technician.status == TechnicianStatus.ACTIVE,
                                Technician.is_online == True,
                            )
                        )).scalars().all())

                        # Count how many times each online tech was tried for this booking
                        tech_attempt_counts = {}
                        for a in past_assignments:
                            if a.technician_id in online_tech_ids:
                                tech_attempt_counts[a.technician_id] = tech_attempt_counts.get(a.technician_id, 0) + 1

                        # Exhausted = every currently-online tech has been tried >= 2 times
                        all_exhausted = (
                            len(online_tech_ids) > 0
                            and all(tech_attempt_counts.get(tid, 0) >= 2 for tid in online_tech_ids)
                        )

                        if all_exhausted:
                            # Already escalated? Check status log
                            already_escalated = (await db.execute(
                                select(BookingStatusLog).where(
                                    BookingStatusLog.booking_id == booking.id,
                                    BookingStatusLog.notes.ilike("%NEEDS_MANUAL_ASSIGN%"),
                                )
                            )).scalars().first()
                            if not already_escalated:
                                await escalate_to_manual(db, booking, len(past_assignments))
                            continue

                        # ── Try to find next available online tech (exclude rejecters) ──
                        # Build exclude list from this session
                        try:
                            # Use _pick_best_technician_online with exclude logic
                            # We need to exclude all previously rejected techs
                            from sqlalchemy import not_
                            # Exclude only techs tried >= 2 times (exhausted their quota).
                            # Techs tried only once can still receive a 2nd attempt.
                            exhausted_tech_ids = {
                                tid for tid in rejected_tech_ids
                                if tech_attempt_counts.get(tid, 0) >= 2
                            }
                            candidates_q = (await db.execute(
                                select(Technician).where(
                                    Technician.status == TechnicianStatus.ACTIVE,
                                    Technician.is_online == True,
                                    not_(Technician.id.in_(exhausted_tech_ids)) if exhausted_tech_ids else True,
                                )
                            )).scalars().all()

                            if not candidates_q:
                                # All online techs already tried — check if 2 rounds done
                                if len(past_assignments) >= online_count * 2:
                                    already_escalated = (await db.execute(
                                        select(BookingStatusLog).where(
                                            BookingStatusLog.booking_id == booking.id,
                                            BookingStatusLog.notes.ilike("%NEEDS_MANUAL_ASSIGN%"),
                                        )
                                    )).scalars().first()
                                    if not already_escalated:
                                        await escalate_to_manual(db, booking, len(past_assignments))
                                continue

                            # Pick best from remaining candidates
                            # candidates_q is already filtered (excludes rejected techs).
                            # Pick the first candidate — _pick_best_technician_online
                            # re-fetches all online techs internally, so we pass the
                            # first rejected tech ID just as a hint; the real filter
                            # is the candidates_q list we built above.
                            # Simplest correct approach: pick best from candidates_q
                            # by rating + workload (mirrors _pick_best_technician_online scoring).
                            if not candidates_q:
                                continue
                            from app.api.v1.routes.assignments import _get_active_workload, _haversine_km
                            from app.models.customer import CustomerAddress
                            from app.models.technician import TechnicianSkill
                            _booking_lat, _booking_lng = None, None
                            if booking.address_id:
                                _addr = (await db.execute(
                                    select(CustomerAddress).where(CustomerAddress.id == booking.address_id)
                                )).scalar_one_or_none()
                                if _addr and getattr(_addr, "latitude", None):
                                    _booking_lat, _booking_lng = _addr.latitude, _addr.longitude
                            # Build skill match set for this booking's service
                            _skill_match_ids = set()
                            if booking.service_id:
                                _skill_rows = (await db.execute(
                                    select(TechnicianSkill).where(TechnicianSkill.service_id == booking.service_id)
                                )).scalars().all()
                                _skill_match_ids = {r.technician_id for r in _skill_rows}
                            # If skill match required, filter candidates
                            _filtered_cands = candidates_q
                            if rules.require_skill_match and _skill_match_ids:
                                _filtered_cands = [t for t in candidates_q if t.id in _skill_match_ids]
                                if not _filtered_cands:
                                    _filtered_cands = candidates_q  # fallback: ignore skill if none match
                            scored_cands = []
                            for _t in _filtered_cands:
                                _wl = await _get_active_workload(db, _t.id)
                                if _wl >= rules.max_active_bookings:
                                    continue
                                _s = _t.rating * 20 + max(0, 30 - _wl * 10)
                                if _t.id in _skill_match_ids:
                                    _s += 50  # skill match bonus (same as _pick_best_technician_online)
                                if _booking_lat and _t.last_lat:
                                    _s += max(0, 30 - _haversine_km(_t.last_lat, _t.last_lng, _booking_lat, _booking_lng))
                                scored_cands.append((_s, _t, _wl))
                            if not scored_cands:
                                continue
                            scored_cands.sort(key=lambda x: x[0], reverse=True)
                            score, best_tech, _ = scored_cands[0]

                            _logger.info(f"[AUTO-RETRY] Booking {booking.booking_number} → {best_tech.name}")
                            await _apply_assignment(
                                db, booking, best_tech, AssignmentType.AUTO,
                                _system_user_id,
                                f"Auto-retry assignment (attempt {len(past_assignments)+1})",
                                score,
                                rules.response_timeout_minutes,
                            )
                            new_asgn = (await db.execute(
                                select(AssignmentHistory).where(
                                    AssignmentHistory.booking_id == booking.id,
                                    AssignmentHistory.technician_id == best_tech.id,
                                    AssignmentHistory.status == AssignmentStatus.ASSIGNED,
                                ).order_by(AssignmentHistory.created_at.desc())
                            )).scalars().first()
                            if new_asgn:
                                track_task(_timeout_watcher(
                                    str(new_asgn.id), bid, str(best_tech.id),
                                    rules.response_timeout_minutes,
                                ))
                        except Exception as _ae:
                            _logger.warning(f"[AUTO-RETRY] No candidate for {booking.booking_number}: {_ae}", exc_info=True)

                    except Exception as _be:
                        _logger.warning(f"[AUTO-RETRY] Error processing {booking.booking_number}: {_be}", exc_info=True)

        except asyncio.CancelledError:
            # BUG FIX: re-raise immediately on shutdown. The old code caught
            # this via a bare `finally: await asyncio.sleep(86400)` below,
            # which is itself an *uncancelled* fresh await -- asyncio
            # cancellation is one-shot, so that 24h sleep just ran quietly
            # in the background, and main.py's `await task` in lifespan
            # shutdown blocked on it indefinitely. That's why Ctrl+C hung
            # even on a totally fresh start: this task begins life inside
            # the first `await asyncio.sleep(sleep_seconds)` immediately
            # after boot. Re-raising here lets shutdown's `await task`
            # complete instantly instead of waiting up to 24h.
            raise
        except Exception as e:
            _logger.warning(f"[AUTO-RETRY] Loop error: {e}", exc_info=True)
            # Brief backoff after an unexpected error so we don't tight-loop.
            # The regular 9 AM IST target is recalculated at the top of the
            # loop regardless, so no extra 24h sleep belongs here.
            await asyncio.sleep(60)




async def _pay_later_reminder_sweep():
    """
    Background task: runs every 15 minutes.

    Finds PENDING PAY_LATER payment transactions whose due_collect_at has
    been reached, and reminds the technician + all admin/CCO users to
    collect the payment. Re-reminds every 24h thereafter while the
    transaction is still PENDING (tracked via last_reminder_at).
    """
    import asyncio
    import logging
    from datetime import datetime, timezone, timedelta

    _logger = logging.getLogger(__name__)
    SWEEP_INTERVAL_SECONDS = 15 * 60
    RE_REMIND_AFTER = timedelta(hours=24)

    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

            from app.core.database import AsyncSessionLocal
            from app.models.payment import PaymentTransaction, PaymentMethod, PaymentStatus
            from app.models.booking import Booking
            from app.models.technician import Technician
            from app.models.user import User
            from app.models.notification import Notification
            from app.websocket.manager import (
                publish_event, WSEvent, technician_room, ADMIN_BOOKINGS_ROOM,
            )
            from app.utils.fcm import send_simple_push
            from sqlalchemy import select

            now = datetime.now(timezone.utc)

            async with AsyncSessionLocal() as db:
                due_txns = (await db.execute(
                    select(PaymentTransaction).where(
                        PaymentTransaction.method == PaymentMethod.PAY_LATER,
                        PaymentTransaction.status == PaymentStatus.PENDING,
                        PaymentTransaction.due_collect_at != None,
                        PaymentTransaction.due_collect_at <= now,
                    )
                )).scalars().all()

                if not due_txns:
                    continue

                admin_users = (await db.execute(
                    select(User).where(
                        User.role.in_(["SUPER_ADMIN", "ADMIN", "CCO"]),
                        User.fcm_token.isnot(None),
                        User.is_active == True,
                    )
                )).scalars().all()

                for txn in due_txns:
                    try:
                        # Skip if reminded within the last 24h
                        last = txn.last_reminder_at
                        if last is not None:
                            last_aware = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                            if now - last_aware < RE_REMIND_AFTER:
                                continue

                        booking = (await db.execute(
                            select(Booking).where(Booking.id == txn.booking_id)
                        )).scalar_one_or_none()
                        if booking is None:
                            continue

                        title = "Payment Collection Due"
                        body = (
                            f"Booking {booking.booking_number}: pay-later payment of "
                            f"₹{txn.amount:.2f} is due for collection."
                        )
                        payload = {
                            "transaction_id":  str(txn.id),
                            "booking_id":      str(booking.id),
                            "booking_number":  booking.booking_number,
                            "amount":          txn.amount,
                            "due_collect_at":  txn.due_collect_at.isoformat() if txn.due_collect_at else None,
                        }

                        recipient_user_ids = []

                        # Technician assigned to the booking
                        technician = None
                        if booking.technician_id:
                            technician = (await db.execute(
                                select(Technician).where(Technician.id == booking.technician_id)
                            )).scalar_one_or_none()
                        if technician:
                            recipient_user_ids.append(technician.user_id)
                            if technician.fcm_token:
                                track_task(send_simple_push(
                                    fcm_token=technician.fcm_token,
                                    title=title, body=body,
                                    data={"type": "PAYMENT_DUE_REMINDER", **{k: str(v) for k, v in payload.items()}},
                                ))
                            track_task(publish_event(
                                technician_room(str(technician.id)), WSEvent.PAYMENT_DUE_REMINDER, payload,
                            ))

                        # Admin / CCO users
                        for admin_user in admin_users:
                            recipient_user_ids.append(admin_user.id)
                            track_task(send_simple_push(
                                fcm_token=admin_user.fcm_token,
                                title=title, body=body,
                                data={"type": "PAYMENT_DUE_REMINDER", **{k: str(v) for k, v in payload.items()}},
                            ))
                        track_task(publish_event(ADMIN_BOOKINGS_ROOM, WSEvent.PAYMENT_DUE_REMINDER, payload))

                        # In-app notification rows (bell icon) for every recipient
                        for uid in recipient_user_ids:
                            db.add(Notification(
                                user_id=uid, title=title, body=body,
                                channel="PUSH", data=payload,
                            ))

                        txn.last_reminder_at = now
                        await db.commit()
                        _logger.info(f"[PAY-LATER] Reminder sent for booking {booking.booking_number}")
                    except Exception as _te:
                        await db.rollback()
                        _logger.warning(f"[PAY-LATER] Error reminding for txn {txn.id}: {_te}", exc_info=True)

        except asyncio.CancelledError:
            # Same one-shot-cancellation lesson as _auto_retry_unassigned_bookings:
            # re-raise immediately so lifespan shutdown's `await task` returns
            # right away instead of blocking on the next sleep.
            raise
        except Exception as e:
            _logger.warning(f"[PAY-LATER] Loop error: {e}", exc_info=True)
            await asyncio.sleep(60)




async def _safe_db_patches():
    """
    Idempotent DB patches on every startup — runs raw SQL via psycopg2 in
    autocommit mode so ALTER TYPE ADD VALUE works (PostgreSQL restriction:
    ADD VALUE cannot run inside a transaction).

    Patches:
      P1: paymentstatus.CANCELLED
      P2: all bookingstatus enum values that VPS migrations missed
      P3: bookings columns (coupon_id, coupon_code, coupon_discount, city_id)
    """
    import subprocess, sys, os
    from app.core.config import settings as _s

    # Build the psql command — use the DATABASE_URL directly.
    # Strip +asyncpg driver qualifier if present.
    _url = _s.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    _sql = """
ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'CANCELLED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PENDING_VERIFICATION';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'TECHNICIAN_ACCEPTED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'INVOICE_GENERATED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAYMENT_PENDING';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_STARTED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'WORK_PAUSED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'REFUND_INITIATED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'PAID';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CLOSED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'SETTLED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'QUOTATION_APPROVED';
ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'CANCELLATION_REQUESTED';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_id       UUID;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_code     VARCHAR(50);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS coupon_discount FLOAT DEFAULT 0.0;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS city_id         UUID;
"""

    try:
        result = subprocess.run(
            ["psql", _url, "-c", _sql],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print("[OK] safe_db_patches: enum values and bookings columns ensured")
        else:
            # psql not available or connection failed — log and continue
            print(f"[WARN] safe_db_patches psql: {result.stderr.strip()[:200]}")
    except FileNotFoundError:
        print("[WARN] safe_db_patches: psql not found, skipping direct patches")
    except Exception as e:
        print(f"[WARN] safe_db_patches: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ─────────────────────────────────────────────
    await _auto_migrate()
    await _safe_db_patches()
    await _seed_admin()
    await start_redis_subscriber()
    import asyncio

    # BUG FIX: these were previously fired with asyncio.ensure_future() and
    # never stored anywhere, so there was nothing for shutdown to cancel.
    # On Ctrl+C, uvicorn runs this lifespan's shutdown half and then tries
    # to close the event loop -- but these two `while True` background
    # loops (each holding a checked-out DB connection while they sleep/run)
    # were left dangling. On Windows in particular (ProactorEventLoop +
    # WatchFiles' multiprocessing reloader), orphaned tasks holding open
    # asyncpg sockets are a common cause of the process hanging on Ctrl+C
    # instead of actually exiting. Track the task handles so shutdown can
    # cancel them cleanly, same pattern already used for the Redis
    # subscriber below.
    auto_offline_task = asyncio.ensure_future(_auto_offline_stale_technicians())
    auto_retry_task    = asyncio.ensure_future(_auto_retry_unassigned_bookings())
    pay_later_reminder_task = asyncio.ensure_future(_pay_later_reminder_sweep())

    yield
    # ── shutdown ─────────────────────────────────────────────
    await stop_redis_subscriber()

    for task in (auto_offline_task, auto_retry_task, pay_later_reminder_task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # BUG FIX: the asyncpg connection pool (via SQLAlchemy's async engine)
    # was never disposed. Leftover open sockets are another common cause
    # of a hung Ctrl+C / process that won't die on Windows -- dispose() here
    # closes every pooled connection cleanly before the loop shuts down.
    # Cancel any other outstanding fire-and-forget tasks (timeout watchers,
    # WS event publishes, push notifications, etc.) scheduled via track_task()
    # from route handlers. Without this, a task like _timeout_watcher -- which
    # can be asleep for several minutes waiting on a technician response --
    # is left dangling on shutdown, holding the event loop open.
    await cancel_all_background_tasks()

    from app.core.database import engine
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(ws_router)  # WebSocket endpoints (no prefix — uses /ws/... paths)


# ── Global 500 handler ────────────────────────────────────────────────────────
# FastAPI's CORSMiddleware only injects Access-Control-Allow-Origin on
# responses it processes normally.  When an unhandled exception produces a 500,
# Starlette's ServerErrorMiddleware fires *before* CORS can add its headers,
# so the browser sees a CORS error instead of the real 500.  This handler runs
# inside the middleware stack (after CORS), so the CORS headers are already
# present on the request object by the time we return a JSONResponse — but we
# add them explicitly here as a belt-and-suspenders safety net so the browser
# always receives them, even during a crash.
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    import logging, traceback
    logging.getLogger("uvicorn.error").error(
        "Unhandled exception: %s\n%s", exc, traceback.format_exc()
    )
    origin = request.headers.get("origin", "")
    cors_headers: dict = {}
    if origin in settings.ALLOWED_ORIGINS:
        cors_headers = {
            "Access-Control-Allow-Origin":      origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods":     "*",
            "Access-Control-Allow-Headers":     "*",
            "Vary":                             "Origin",
        }
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=cors_headers,
    )


@app.get("/health")
def health_check():
    return {"status": "ok", "app": settings.APP_NAME}
