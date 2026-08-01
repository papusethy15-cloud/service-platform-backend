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
            # Dynamically resolves the current head revision from the migration
            # scripts so this check never needs manual updates when new migrations
            # are added. Falls back to running alembic if anything goes wrong.
            try:
                import subprocess as _sp
                backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ini_path = os.path.join(backend_dir, "alembic.ini")
                # Get the actual head revision from alembic
                _head_result = _sp.run(
                    ["python3", "-m", "alembic", "-c", ini_path, "heads", "--resolve-dependencies"],
                    capture_output=True, text=True, timeout=15, cwd=backend_dir
                )
                _head_rev = None
                if _head_result.returncode == 0 and _head_result.stdout.strip():
                    # Output is like "075 (head)" — extract the revision
                    _head_line = _head_result.stdout.strip().split("\n")[-1]
                    _head_rev = _head_line.split()[0].strip()

                if _head_rev:
                    _pg_url = _s.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
                    _vcheck = _sp.run(
                        ["psql", _pg_url, "-t", "-A", "-c",
                         f"SELECT COUNT(*) FROM alembic_version WHERE version_num = '{_head_rev}'"],
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
        except SystemExit as e:
            # alembic/Click calls sys.exit() on completion or error.
            # In a ThreadPoolExecutor thread, SystemExit is re-raised as-is.
            # Treat exit code 0 as success, anything else as a warning.
            if e.code == 0 or e.code is None:
                print("[OK] Auto-migrate: alembic completed (exit 0)")
            else:
                print(f"[WARN] Auto-migrate: alembic exited with code {e.code}")
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
    Also auto-checkouts any open attendance session for those technicians
    (regardless of date) so stale LIVE sessions are properly closed.
    This handles phone-off, app-kill, no internet scenarios.
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    while True:
        try:
            await asyncio.sleep(120)  # check every 2 minutes
            from app.core.database import AsyncSessionLocal
            from app.models.technician import Technician
            from app.models.attendance import Attendance
            from sqlalchemy import select
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

                    # ── Auto-checkout any open attendance session for this technician ──
                    # Find open session (check_in set, check_out null) on ANY date.
                    # This handles the case where the technician was never checked out
                    # due to app-kill, network loss, or server restart.
                    open_att = (await db.execute(
                        select(Attendance).where(
                            Attendance.technician_id == tech.id,
                            Attendance.check_in != None,
                            Attendance.check_out == None,
                        )
                    )).scalars().all()

                    now_utc = datetime.now(timezone.utc)
                    for att in open_att:
                        check_in_aware = att.check_in
                        if check_in_aware.tzinfo is None:
                            check_in_aware = check_in_aware.replace(tzinfo=timezone.utc)
                        elapsed = (now_utc - check_in_aware).total_seconds()
                        att.accumulated_seconds = (att.accumulated_seconds or 0) + max(0, int(elapsed))
                        att.check_out = now_utc
                        att.notes = (att.notes or "") + " [Auto-checked out: technician went offline]"
                        print(
                            f"[AUTO-OFFLINE] Auto-checkout attendance id={att.id} "
                            f"date={att.date} tech={tech.name} elapsed={elapsed:.0f}s"
                        )

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
                    _get_default_rules, _apply_assignment, _two_phase_watcher,
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
                            if new_asgn and new_asgn.response_deadline:
                                track_task(_two_phase_watcher(
                                    str(new_asgn.id), bid, str(best_tech.id),
                                    new_asgn.response_deadline,
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
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS appliance_id    UUID;
ALTER TYPE bookingsource ADD VALUE IF NOT EXISTS 'CALL_CENTER';
ALTER TYPE bookingsource ADD VALUE IF NOT EXISTS 'WALK_IN';
ALTER TYPE bookingsource ADD VALUE IF NOT EXISTS 'FRANCHISE';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(500);
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS coupon_id        UUID;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS coupon_code      VARCHAR(50);
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS coupon_discount  FLOAT DEFAULT 0.0;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS services_total   FLOAT DEFAULT 0.0;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS labour_charges   FLOAT DEFAULT 0.0;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS service_charges  FLOAT DEFAULT 0.0;
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS adjustment_amount FLOAT DEFAULT 0.0;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS payment_id UUID;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS technician_id UUID;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS min_order_amount FLOAT DEFAULT 0.0;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS max_discount_amount FLOAT;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS customer_mobile_numbers TEXT[];
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS service_ids UUID[];
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS category_ids UUID[];
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS per_customer_limit INTEGER;
ALTER TABLE amc_plans ADD COLUMN IF NOT EXISTS plan_type VARCHAR(30) DEFAULT 'GOLD';
ALTER TABLE amc_subscriptions ADD COLUMN IF NOT EXISTS visits_remaining INTEGER DEFAULT 0;
ALTER TABLE warranties ADD COLUMN IF NOT EXISTS parts_covered TEXT;
ALTER TABLE warranty_claims ADD COLUMN IF NOT EXISTS booking_id UUID;
ALTER TABLE coupon_usages ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE SET NULL;
ALTER TABLE warranty_claims ADD COLUMN IF NOT EXISTS approved_by UUID;
ALTER TABLE warranty_claims ADD COLUMN IF NOT EXISTS rejected_by UUID;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS hsn_code VARCHAR(20);
ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS city_id UUID;
-- created_at: ensure column exists WITH default on all raw-Base tables
-- Step 1: add column if missing (for tables that never had it)
ALTER TABLE appliance_brands ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE appliance_service_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE appliance_types ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE booking_part_usage ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE brand_categories ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE cco_attendance ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE cco_salary_settlements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_group_part_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_group_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE coupon_usages ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE customer_appliances ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE direct_sales ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE franchises ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_brands ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_categories ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_reorder_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE salary_settlements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE sla_policies ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE technician_stock_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE transfer_challans ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
-- Step 2: set default on column if it exists but has no default (migration created it without one)
ALTER TABLE appliance_brands ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE appliance_service_history ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE appliance_types ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE attendance ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE booking_part_usage ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE brand_categories ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE cco_attendance ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE cco_salary_settlements ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE commission_group_part_rules ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE commission_group_rules ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE commission_groups ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE commission_rules ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE commissions ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE coupon_usages ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE coupons ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE customer_appliances ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE direct_sales ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE franchises ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE inventory_brands ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE inventory_categories ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE inventory_items ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE inventory_reorder_rules ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE leave_requests ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE notification_templates ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE refunds ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE salary_settlements ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE sla_policies ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE stock_movements ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE technician_stock_logs ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE transfer_challans ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE wallet_transactions ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE wallets ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE warehouses ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE withdrawal_requests ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE notifications ALTER COLUMN created_at SET DEFAULT now();
-- Step 3: fill any NULL rows left from before the default was set
UPDATE appliance_brands SET created_at = now() WHERE created_at IS NULL;
UPDATE appliance_service_history SET created_at = now() WHERE created_at IS NULL;
UPDATE appliance_types SET created_at = now() WHERE created_at IS NULL;
UPDATE attendance SET created_at = now() WHERE created_at IS NULL;
UPDATE booking_part_usage SET created_at = now() WHERE created_at IS NULL;
UPDATE brand_categories SET created_at = now() WHERE created_at IS NULL;
UPDATE cco_attendance SET created_at = now() WHERE created_at IS NULL;
UPDATE cco_salary_settlements SET created_at = now() WHERE created_at IS NULL;
UPDATE commission_group_part_rules SET created_at = now() WHERE created_at IS NULL;
UPDATE commission_group_rules SET created_at = now() WHERE created_at IS NULL;
UPDATE commission_groups SET created_at = now() WHERE created_at IS NULL;
UPDATE commission_rules SET created_at = now() WHERE created_at IS NULL;
UPDATE commissions SET created_at = now() WHERE created_at IS NULL;
UPDATE coupon_usages SET created_at = now() WHERE created_at IS NULL;
UPDATE coupons SET created_at = now() WHERE created_at IS NULL;
UPDATE customer_appliances SET created_at = now() WHERE created_at IS NULL;
UPDATE direct_sales SET created_at = now() WHERE created_at IS NULL;
UPDATE franchises SET created_at = now() WHERE created_at IS NULL;
UPDATE inventory_brands SET created_at = now() WHERE created_at IS NULL;
UPDATE inventory_categories SET created_at = now() WHERE created_at IS NULL;
UPDATE inventory_items SET created_at = now() WHERE created_at IS NULL;
UPDATE inventory_reorder_rules SET created_at = now() WHERE created_at IS NULL;
UPDATE leave_requests SET created_at = now() WHERE created_at IS NULL;
UPDATE notification_templates SET created_at = now() WHERE created_at IS NULL;
UPDATE refunds SET created_at = now() WHERE created_at IS NULL;
UPDATE salary_settlements SET created_at = now() WHERE created_at IS NULL;
UPDATE sla_policies SET created_at = now() WHERE created_at IS NULL;
UPDATE stock_movements SET created_at = now() WHERE created_at IS NULL;
UPDATE technician_stock_logs SET created_at = now() WHERE created_at IS NULL;
UPDATE transfer_challans SET created_at = now() WHERE created_at IS NULL;
UPDATE wallet_transactions SET created_at = now() WHERE created_at IS NULL;
UPDATE wallets SET created_at = now() WHERE created_at IS NULL;
UPDATE warehouses SET created_at = now() WHERE created_at IS NULL;
UPDATE withdrawal_requests SET created_at = now() WHERE created_at IS NULL;
UPDATE notifications SET created_at = now() WHERE created_at IS NULL;
ALTER TABLE appliance_brands ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE appliance_service_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE appliance_types ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE booking_part_usage ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE brand_categories ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE cco_attendance ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE cco_salary_settlements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_group_part_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_group_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_groups ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commission_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE commissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE coupon_usages ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE customer_appliances ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE direct_sales ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE franchises ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_brands ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_categories ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE inventory_reorder_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE salary_settlements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE sla_policies ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE technician_stock_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE transfer_challans ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE warehouses ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT FALSE;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS gateway_refund_id VARCHAR(200);
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS processed_by UUID;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS refund_method VARCHAR(30);
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_earned FLOAT DEFAULT 0.0;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_withdrawn FLOAT DEFAULT 0.0;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS discount_type VARCHAR(20);
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS discount_value FLOAT;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS usage_limit INTEGER;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS used_count INTEGER DEFAULT 0;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP WITH TIME ZONE;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP WITH TIME ZONE;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();
ALTER TABLE amc_plans ADD COLUMN IF NOT EXISTS visit_count INTEGER DEFAULT 0;
ALTER TABLE amc_plans ADD COLUMN IF NOT EXISTS duration_months INTEGER DEFAULT 12;
ALTER TABLE amc_plans ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE amc_plans ADD COLUMN IF NOT EXISTS appliance_types TEXT;
ALTER TABLE amc_subscriptions ADD COLUMN IF NOT EXISTS amount_paid FLOAT DEFAULT 0.0;
ALTER TABLE amc_subscriptions ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'ACTIVE';
ALTER TABLE amc_subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS gst_percent FLOAT DEFAULT 18.0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS cost_price FLOAT DEFAULT 0.0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS selling_price FLOAT DEFAULT 0.0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS mrp FLOAT DEFAULT 0.0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS current_stock INTEGER DEFAULT 0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS reserved_stock INTEGER DEFAULT 0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS min_stock_level INTEGER DEFAULT 0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS reorder_qty INTEGER DEFAULT 0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS is_consumable BOOLEAN DEFAULT FALSE;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS is_serialised BOOLEAN DEFAULT FALSE;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
CREATE TABLE IF NOT EXISTS callback_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mobile VARCHAR(20) NOT NULL,
    name VARCHAR(150),
    message TEXT,
    source VARCHAR(30) DEFAULT 'CHATBOT',
    status VARCHAR(30) DEFAULT 'PENDING',
    admin_notes TEXT,
    called_at TIMESTAMP WITHOUT TIME ZONE,
    domain_id UUID,
    page_url VARCHAR(500),
    ip_address VARCHAR(64),
    user_agent VARCHAR(500),
    location VARCHAR(255),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE
);
-- ── P-COMMISSIONS: drop stale columns left by migration 001 ─────────────────
-- migration 001 created commissions with `amount FLOAT NOT NULL` plus
-- commission_type / is_active / updated_at.  The model now uses base_amount
-- + commission_amount instead.  The stale `amount NOT NULL` causes:
--   NotNullViolationError on every POST /bookings/{id}/settle
-- DROP COLUMN IF EXISTS is idempotent — completely safe on every startup.
ALTER TABLE commissions DROP COLUMN IF EXISTS amount;
ALTER TABLE commissions DROP COLUMN IF EXISTS commission_type;
ALTER TABLE commissions DROP COLUMN IF EXISTS is_active;
ALTER TABLE commissions DROP COLUMN IF EXISTS updated_at;
-- ── P-WALLETS: fix user_id NOT NULL — technician wallets have no user_id ────
-- Original migration 001 created wallets with user_id NOT NULL + UNIQUE.
-- The Wallet model has user_id nullable=True (technicians use technician_id).
-- This schema drift causes wallet creation to fail with NotNullViolationError.
ALTER TABLE wallets ALTER COLUMN user_id DROP NOT NULL;
-- Drop the UNIQUE constraint on user_id so technician wallets (user_id=NULL)
-- don't conflict with each other (NULL != NULL in unique indexes, but
-- some Postgres versions block multiple NULLs on a UNIQUE column).
DO $$ BEGIN
  ALTER TABLE wallets DROP CONSTRAINT IF EXISTS wallets_user_id_key;
EXCEPTION WHEN others THEN NULL; END $$;
-- Add a partial unique index instead: unique only when user_id IS NOT NULL
DO $$ BEGIN
  CREATE UNIQUE INDEX wallets_user_id_unique
    ON wallets(user_id) WHERE user_id IS NOT NULL;
EXCEPTION WHEN duplicate_table THEN NULL; END $$;
-- Ensure wallet id has a default (some VPS tables missing gen_random_uuid default)
ALTER TABLE wallets ALTER COLUMN id SET DEFAULT gen_random_uuid();
-- ── P-WALLET-TRANSACTIONS: fix legacy `type` NOT NULL column ────────────────
-- VPS wallet_transactions has: type VARCHAR(20) NOT NULL (legacy col)
-- The model uses `transaction_type` instead. Back-fill `type` from
-- `transaction_type` for any existing rows where type IS NULL, and
-- set a DEFAULT so future raw inserts don't fail.
UPDATE wallet_transactions SET type = transaction_type
  WHERE type IS NULL AND transaction_type IS NOT NULL;
UPDATE wallet_transactions SET type = 'CREDIT'
  WHERE type IS NULL;
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


async def _backfill_technician_wallets():
    """
    Startup backfill — runs once per restart, fully idempotent:
    1. Creates a zero-balance wallet for every technician who doesn't have one.
    2. For any Commission row that is PAID but whose credit is NOT reflected in
       the wallet (wallet balance < expected from PAID commissions), re-credits
       the missing amount and logs a WalletTransaction so the history is correct.

    This covers the window where pay_commission silently skipped the credit
    because the wallet didn't exist yet.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.technician import Technician
        from app.models.wallet import Wallet, WalletTransaction
        from app.models.commission import Commission
        from sqlalchemy import select, func
        from datetime import datetime, timezone

        async with AsyncSessionLocal() as db:
            # ── Step 1: create missing wallets ──────────────────────────────
            techs = (await db.execute(select(Technician))).scalars().all()
            created = 0
            for tech in techs:
                w = (await db.execute(
                    select(Wallet).where(Wallet.technician_id == tech.id)
                )).scalar_one_or_none()
                if not w:
                    db.add(Wallet(technician_id=tech.id, user_id=tech.user_id,
                                  balance=0.0, total_earned=0.0, total_withdrawn=0.0))
                    created += 1
            if created:
                await db.flush()
                print(f"[OK] backfill_wallets: created {created} missing wallet(s)")

            # ── Step 2: re-credit PAID commissions that wallet never received ─
            # For each technician, sum all PAID commission_amounts and compare to
            # sum of CREDIT wallet transactions referencing those commissions.
            # We detect the gap via wallet_transactions: if a PAID commission row
            # has no matching WalletTransaction with description containing its
            # item_name/booking_id, we re-credit it.
            #
            # Simpler safe approach: find PAID commissions where the technician's
            # wallet total_earned < sum(PAID commissions) for that technician.
            # Re-credit the difference once, tagged as backfill.

            paid_rows = (await db.execute(
                select(Commission).where(Commission.status == "PAID")
            )).scalars().all()

            # Group by technician
            from collections import defaultdict
            by_tech = defaultdict(list)
            for c in paid_rows:
                by_tech[str(c.technician_id)].append(c)

            recredited = 0
            for tid_str, comms in by_tech.items():
                from uuid import UUID
                tid = UUID(tid_str)
                wallet = (await db.execute(
                    select(Wallet).where(Wallet.technician_id == tid)
                )).scalar_one_or_none()
                if not wallet:
                    continue  # just created above — will be correct going forward

                expected_earned = round(sum(c.commission_amount or 0 for c in comms), 2)
                actual_earned   = round(float(wallet.total_earned or 0), 2)

                gap = round(expected_earned - actual_earned, 2)
                if gap > 0:
                    # Wallet is missing some credits — top it up
                    balance_before = wallet.balance or 0
                    wallet.balance      = round(balance_before + gap, 2)
                    wallet.total_earned = round(actual_earned   + gap, 2)
                    db.add(WalletTransaction(
                        wallet_id=wallet.id,
                        transaction_type="CREDIT",
                        amount=gap,
                        balance_before=balance_before,
                        balance_after=wallet.balance,
                        description=f"[BACKFILL] Re-credit ₹{gap} for {len(comms)} PAID commission(s) "
                                    f"that were marked PAID before wallet existed",
                        status="SUCCESS",
                    ))
                    recredited += 1
                    print(f"[OK] backfill_wallets: re-credited ₹{gap} to technician {tid_str} "
                          f"(wallet total_earned was ₹{actual_earned}, expected ₹{expected_earned})")

            await db.commit()
            if not created and not recredited:
                print("[OK] backfill_wallets: all wallets up-to-date, nothing to do")

    except Exception as e:
        print(f"[WARN] backfill_wallets: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ─────────────────────────────────────────────
    await _auto_migrate()
    await _safe_db_patches()
    await _seed_admin()
    await _backfill_technician_wallets()
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


# ── Global exception handlers ─────────────────────────────────────────────────
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

def _cors_headers(request: Request) -> dict:
    """Return CORS headers for the request's origin if it is in the allowed list."""
    origin = request.headers.get("origin", "")
    if origin in settings.ALLOWED_ORIGINS:
        return {
            "Access-Control-Allow-Origin":      origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods":     "*",
            "Access-Control-Allow-Headers":     "*",
            "Vary":                             "Origin",
        }
    return {}

# ── IntegrityError → 400 (duplicate key, FK violation, etc.) ──────────────────
# Catches sqlalchemy.exc.IntegrityError (wraps asyncpg UniqueViolationError,
# ForeignKeyViolationError, etc.) globally so every route gets a proper 400
# with a human-readable message instead of a 500.
from sqlalchemy.exc import IntegrityError as _SAIntegrityError

@app.exception_handler(_SAIntegrityError)
async def _integrity_error_handler(request: Request, exc: _SAIntegrityError) -> JSONResponse:
    import logging
    logging.getLogger("uvicorn.error").warning("IntegrityError on %s: %s", request.url.path, exc.orig)
    detail_raw = str(exc.orig) if exc.orig else str(exc)
    # Produce a friendly message based on the constraint name
    if "users_mobile_key" in detail_raw or ("mobile" in detail_raw and "unique" in detail_raw.lower()):
        detail = "This mobile number is already registered. Please use a different mobile number."
    elif "users_email_key" in detail_raw or ("email" in detail_raw and "unique" in detail_raw.lower()):
        detail = "This email address is already registered. Please use a different email."
    elif "unique" in detail_raw.lower() or "duplicate key" in detail_raw.lower():
        detail = f"A record with these details already exists. {detail_raw}"
    elif "foreign key" in detail_raw.lower() or "violates foreign key" in detail_raw.lower():
        detail = "Referenced record does not exist. Please check your input."
    else:
        detail = f"Database constraint error: {detail_raw}"
    return JSONResponse(
        status_code=400,
        content={"detail": detail},
        headers=_cors_headers(request),
    )

from fastapi import HTTPException as _HTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_handler
from fastapi.exception_handlers import request_validation_exception_handler as _default_validation_handler
from fastapi.exceptions import RequestValidationError as _RequestValidationError

@app.exception_handler(_HTTPException)
async def _http_exception_handler_cors(request: Request, exc: _HTTPException) -> JSONResponse:
    """Override FastAPI's default HTTPException handler to always inject CORS headers."""
    response = await _default_http_handler(request, exc)
    for k, v in _cors_headers(request).items():
        response.headers[k] = v
    return response

@app.exception_handler(_RequestValidationError)
async def _validation_exception_handler_cors(request: Request, exc: _RequestValidationError) -> JSONResponse:
    """Override FastAPI's default validation error handler to always inject CORS headers."""
    response = await _default_validation_handler(request, exc)
    for k, v in _cors_headers(request).items():
        response.headers[k] = v
    return response

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    import logging, traceback
    logging.getLogger("uvicorn.error").error(
        "Unhandled exception: %s\n%s", exc, traceback.format_exc()
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=_cors_headers(request),
    )


@app.get("/health")
def health_check():
    return {"status": "ok", "app": settings.APP_NAME}
