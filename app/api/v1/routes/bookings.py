from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
import random, string

from app.core.database import get_db
from app.models.booking import Booking, BookingStatus, BookingStatusLog, BookingSource
from app.models.customer import Customer, CustomerAddress
from app.models.technician import Technician
from app.models.tracking import TrackingLocation
from app.models.domain import Domain
from app.models.quotation import Quotation as QuotationModel, QuotationServiceItem, QuotationPartItem
from app.api.v1.schemas.booking import (
    CreateBookingRequest, UpdateBookingRequest,
    RescheduleBookingRequest, AssignTechnicianRequest,
    CancelBookingRequest
)
from app.api.deps import AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

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
    if booking.status in [BookingStatus.COMPLETED, BookingStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking in {booking.status.value} state")
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_reason = reason
    await _add_status_log(db, booking.id, BookingStatus.CANCELLED, user_id, reason)

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

    # ── Duplicate booking check ────────────────────────────────────────────────
    # Same customer + same service + same address + active (non-cancelled/completed) booking → block
    if customer and not payload.force_duplicate and payload.service_id and payload.address_id:
        # Terminal statuses where re-booking the same service+address SHOULD be allowed.
        # COMPLETED = work done + payment confirmed (customer view of done).
        # PAID, CLOSED, SETTLED = internal admin/commission settlement steps — customer is done.
        # CANCELLED = customer or admin cancelled, always allow re-booking.
        # INVOICE_GENERATED, PAYMENT_PENDING = payment stage — still in-flight, block re-booking.
        REBOOKABLE_STATUSES = [
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.PAID,
            BookingStatus.CLOSED,
            BookingStatus.SETTLED,
        ]
        dup_q = select(Booking).where(
            Booking.customer_id == customer.id,
            Booking.service_id == UUID(payload.service_id),
            Booking.address_id == UUID(payload.address_id),
            Booking.status.notin_(REBOOKABLE_STATUSES)
        )
        duplicate = (await db.execute(dup_q)).scalar_one_or_none()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"DUPLICATE:{duplicate.booking_number}:{duplicate.status.value}")

    # Fetch service for pricing/name (service_id is optional for chatbot/web bookings)
    from app.models.service import Service
    service = None
    if payload.service_id:
        service = (await db.execute(select(Service).where(Service.id == UUID(payload.service_id)))).scalar_one_or_none()

    # Strip timezone info so it's compatible with TIMESTAMP WITHOUT TIME ZONE
    from datetime import timezone as _tz
    sched_date = payload.scheduled_date
    if sched_date and sched_date.tzinfo is not None:
        sched_date = sched_date.replace(tzinfo=None)

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

    booking = Booking(
        booking_number=generate_booking_number(),
        customer_id=customer.id if customer else UUID(current_user["user_id"]),
        service_id=UUID(payload.service_id) if payload.service_id else None,
        service_name=service.name if service else payload.service_name,
        address_id=UUID(payload.address_id) if payload.address_id else None,
        address_line=payload.address_line,
        city=payload.city,
        scheduled_date=sched_date,
        scheduled_slot=payload.scheduled_slot,
        notes=payload.notes,
        appliance_brand=payload.appliance_brand,
        appliance_model=payload.appliance_model,
        source=BookingSource(payload.source),
        status=BookingStatus.CONFIRMED,
        priority=payload.priority or "NORMAL",
        domain_id=UUID(payload.domain_id) if payload.domain_id else None,
        base_amount=base_price,
        discount_amount=coupon_discount,
        total_amount=final_total,
        coupon_id=coupon_id,
        coupon_code=coupon_code,
        coupon_discount=coupon_discount,
    )
    db.add(booking)
    await db.flush()
    await _add_status_log(db, booking.id, BookingStatus.CONFIRMED, current_user["user_id"], "Booking created by admin/CCO")
    await db.commit()
    return success_response(data={"id": str(booking.id), "booking_number": booking.booking_number, "status": booking.status.value}, message="Booking created")

# ── LIST BOOKINGS ──────────────────────────────────────────────
@router.get("", summary="List bookings [Admin/CCO or own]")
async def list_bookings(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = Query(None),
    priority: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.service import Service
    from sqlalchemy import or_, and_, cast, String as SAString
    from datetime import datetime

    # Build base query with LEFT JOINs for customer, service, technician, domain
    q = (
        select(Booking, Customer, Service, Technician, Domain)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .outerjoin(Domain, Domain.id == Booking.domain_id)
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

    # Filters
    if status:
        q = q.where(Booking.status == BookingStatus(status))
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
        q = q.where(Booking.scheduled_date >= datetime.fromisoformat(date_from))
    if date_to:
        q = q.where(Booking.scheduled_date <= datetime.fromisoformat(date_to))

    # Count using same joined + filtered query
    count_q = select(func.count(Booking.id)).select_from(
        select(Booking, Customer, Service, Technician, Domain)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .outerjoin(Domain, Domain.id == Booking.domain_id)
        .filter(q.whereclause)
        .subquery()
    ) if q.whereclause is not None else select(func.count(Booking.id)).select_from(Booking)
    try:
        total = (await db.execute(count_q)).scalar_one()
    except Exception:
        total = len((await db.execute(q)).all())

    rows = (await db.execute(
        q.order_by(Booking.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    )).all()

    items = []
    for idx, (b, cust, svc, tech, domain) in enumerate(rows):
        svc_name = b.service_name or (svc.name if svc else None) or "—"
        items.append({
            "id": str(b.id),
            "booking_number": b.booking_number,
            "status": b.status.value,
            "priority": b.priority or "NORMAL",
            "source": b.source.value if b.source else "—",
            "scheduled_date": b.scheduled_date.isoformat() if b.scheduled_date else None,
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
            "appliance_brand": b.appliance_brand or None,
            "appliance_model": b.appliance_model or None,
            "notes": b.notes or None,
            "cancelled_reason": b.cancelled_reason or None,
            "city": b.city or None,
            "coupon_code": b.coupon_code,
            "coupon_discount": b.coupon_discount or 0.0,
            "domain_name": domain.name if domain else None,
            "created_at": b.created_at.isoformat() if b.created_at else None,
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
        matched_customer = (
            await db.execute(select(Customer).where(Customer.mobile == phone.strip()))
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
        "timestamp": l.created_at.isoformat(),
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
        select(Booking, Customer, Service, Technician, Domain)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .outerjoin(Domain, Domain.id == Booking.domain_id)
        .where(Booking.id == booking_id)
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    b, cust, svc, tech, domain = row
    svc_name = b.service_name or (svc.name if svc else None) or "—"
    # Build address string from CustomerAddress if address_id exists
    addr_str = None
    addr_label = None
    if b.address_id:
        addr = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == b.address_id))).scalar_one_or_none()
        if addr:
            parts = [p for p in [addr.address_line1, addr.city, addr.state, addr.pincode] if p]
            addr_str   = ", ".join(parts) if parts else None
            addr_label = addr.label
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
        "status": b.status.value, "source": b.source.value if b.source else None,
        "scheduled_date": b.scheduled_date.isoformat() if b.scheduled_date else None,
        "scheduled_slot": b.scheduled_slot,
        "notes": b.notes,
        "appliance_brand": b.appliance_brand, "appliance_model": b.appliance_model,
        "base_amount": b.base_amount or 0, "discount_amount": b.discount_amount or 0,
        "gst_amount": b.gst_amount or 0, "total_amount": b.total_amount or 0,
        "priority": b.priority or "NORMAL",
        "city": b.city or (addr_str.split(",")[1].strip() if addr_str and "," in addr_str else None),
        "cancelled_reason": b.cancelled_reason,
        "domain_name": domain.name if domain else None,
        "domain_id": str(b.domain_id) if b.domain_id else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
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
@router.post("/{booking_id}/reschedule", summary="Reschedule booking")
async def reschedule_booking(booking_id: UUID, payload: RescheduleBookingRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    from datetime import timezone as _tz2
    resched_date = payload.scheduled_date
    if resched_date and resched_date.tzinfo is not None:
        resched_date = resched_date.replace(tzinfo=None)
    booking.scheduled_date = resched_date
    if payload.scheduled_slot: booking.scheduled_slot = payload.scheduled_slot
    booking.status = BookingStatus.RESCHEDULED
    await _add_status_log(db, booking.id, BookingStatus.RESCHEDULED, current_user["user_id"], payload.reason)
    await db.commit()
    return success_response(message="Booking rescheduled")

# ── ASSIGN TECHNICIAN ──────────────────────────────────────────
@router.post("/{booking_id}/assign", summary="Assign technician [Admin/CCO]")
async def assign_technician(booking_id: UUID, payload: AssignTechnicianRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    booking.technician_id = UUID(payload.technician_id)
    booking.status = BookingStatus.ASSIGNED
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
    await _cancel_booking(db, booking, current_user["user_id"], reason)
    await db.commit()
    return success_response(message="Booking cancelled")

@router.post("/{booking_id}/cancel", summary="Cancel booking")
async def cancel_booking(booking_id: UUID, payload: CancelBookingRequest, current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    booking = await _get_booking_or_404(db, booking_id)
    await _cancel_booking(db, booking, current_user["user_id"], payload.reason)
    await db.commit()
    return success_response(message="Booking cancelled")

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
    await _add_status_log(db, booking.id, new_status, current_user["user_id"], notes)
    await db.commit()
    return success_response(data={"status": new_status.value}, message=f"Status updated to {new_status.value}")

@router.post("/{booking_id}/accept",           summary="Accept booking [Technician]")
async def accept(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.ACCEPTED, cu, db, "Accepted by technician")

@router.post("/{booking_id}/reject",           summary="Reject booking [Technician]")
async def reject(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.PENDING, cu, db, "Rejected — reassigning")

@router.post("/{booking_id}/arrived",          summary="Technician arrived")
async def arrived(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.ARRIVED, cu, db, "Technician arrived at location")

@router.post("/{booking_id}/start-inspection", summary="Start inspection")
async def start_inspection(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.INSPECTING, cu, db, "Inspection started")

@router.post("/{booking_id}/start-work",       summary="Start work")
async def start_work(booking_id: UUID, cu: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    return await _transition(booking_id, BookingStatus.IN_PROGRESS, cu, db, "Work started")

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
    return success_response(data=[{"status": l.status.value, "notes": l.notes, "at": l.created_at.isoformat()} for l in logs])


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
                    "recorded_at": location.recorded_at.isoformat(),
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
from pydantic import BaseModel as _BM
from typing import Optional as _Opt
from datetime import datetime as _dt

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
    scheduled_slot:  str          # "10:00 AM – 12:00 PM"
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
    # Find or create customer by mobile
    result = await db.execute(select(Customer).where(Customer.mobile == payload.mobile))
    customer = result.scalar_one_or_none()

    if not customer:
        customer = Customer(
            name=payload.customer_name,
            mobile=payload.mobile,
            email=payload.email,
        )
        db.add(customer)
        await db.flush()

    try:
        sched_dt = _dt.strptime(payload.scheduled_date, "%Y-%m-%d")
    except ValueError:
        sched_dt = _dt.utcnow()

    booking = Booking(
        booking_number=generate_booking_number(),
        customer_id=customer.id,
        service_name=payload.service_name,
        address_line=payload.address,
        city=payload.city,
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
    if booking.status not in [BookingStatus.COMPLETED, BookingStatus.IN_PROGRESS]:
        raise HTTPException(status_code=400, detail=f"Cannot generate invoice from status: {booking.status.value}")
    booking.status = BookingStatus.INVOICE_GENERATED
    await _add_status_log(db, booking.id, BookingStatus.INVOICE_GENERATED, current_user["user_id"], "Invoice generated by admin/CCO")
    await db.commit()
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
                    comm = round(si.total_price * matched_rule.rate / 100, 2)
                else:
                    comm = round(matched_rule.rate * si.quantity, 2)
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
                "match_status": match_status,
            })

        for pi in part_items:
            src = pi.part_source.value if pi.part_source else "OFFICE_STOCK"
            # Find matching part rule: source filter matches or is NULL
            matched_rule = next((
                r for r in group_part_rules
                if (r.part_source_filter is None or r.part_source_filter == src)
                and (r.part_name_match is None or r.part_name_match.lower() in pi.part_name.lower())
            ), None)
            if matched_rule:
                if matched_rule.commission_type == "PERCENTAGE":
                    comm = round(pi.total_price * matched_rule.rate / 100, 2)
                else:
                    comm = round(matched_rule.rate * pi.quantity, 2)
                match_status = "group"
            else:
                comm = None
                match_status = "unmatched"
            line_items.append({
                "type": "PART",
                "quotation_number": q.quotation_number,
                "service_id": None,
                "name": pi.part_name,
                "quantity": pi.quantity,
                "unit_price": pi.unit_price,
                "total_price": pi.total_price,
                "part_source": src,
                "commission_type": matched_rule.commission_type if matched_rule else None,
                "rate": matched_rule.rate if matched_rule else None,
                "commission_amount": comm,
                "match_status": match_status,
            })

    return success_response(data={
        "technician": {"id": str(tech.id), "name": tech.name, "user_id": str(tech.user_id)} if tech else None,
        "commission_group": {"id": str(group.id), "name": group.name} if group else None,
        "line_items": line_items,
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
                comm = round(si.total_price * matched.rate / 100, 2) if matched.commission_type == "PERCENTAGE" else round(matched.rate * si.quantity, 2)
            else:
                comm = None
            line_items.append({"type": "SERVICE", "name": si.service_name, "quantity": si.quantity,
                                "total_price": si.total_price, "part_source": None,
                                "commission_type": matched.commission_type if matched else "PERCENTAGE",
                                "rate": matched.rate if matched else 0, "commission_amount": comm})
        for pi in part_items:
            src = pi.part_source.value if pi.part_source else "OFFICE_STOCK"
            matched = next((r for r in group_part_rules
                            if (r.part_source_filter is None or r.part_source_filter == src)
                            and (r.part_name_match is None or r.part_name_match.lower() in pi.part_name.lower())), None)
            if matched:
                comm = round(pi.total_price * matched.rate / 100, 2) if matched.commission_type == "PERCENTAGE" else round(matched.rate * pi.quantity, 2)
            else:
                comm = None
            line_items.append({"type": "PART", "name": pi.part_name, "quantity": pi.quantity,
                                "total_price": pi.total_price, "part_source": src,
                                "commission_type": matched.commission_type if matched else "PERCENTAGE",
                                "rate": matched.rate if matched else 0, "commission_amount": comm})

    # Apply admin overrides
    override_map = {o["item_index"]: o["commission_amount"] for o in (payload.overrides or [])}
    for idx, item in enumerate(line_items):
        if idx in override_map:
            item["commission_amount"] = override_map[idx]
        if item["commission_amount"] is None:
            item["commission_amount"] = 0  # default to 0 if still unmatched

    total_commission = sum(item["commission_amount"] for item in line_items)

    # Save Commission records per line
    if tech:
        for item in line_items:
            c = Commission(
                technician_id=tech.id,
                booking_id=booking.id,
                base_amount=item["total_price"],
                commission_amount=item["commission_amount"],
                status="APPROVED",
                item_type=item["type"],
                item_name=item["name"],
                item_quantity=item["quantity"],
                part_source=item["part_source"],
                notes=f"Settled: {item['commission_type']} {item['rate']}% on {item['name']}" if item["rate"] else f"Manual override: {item['commission_amount']}",
            )
            db.add(c)

        # Credit wallet — get or create (look up by technician_id first, fallback to user_id)
        wallet = (await db.execute(select(Wallet).where(Wallet.technician_id == tech.id))).scalar_one_or_none()
        if not wallet:
            wallet = Wallet(technician_id=tech.id, user_id=tech.user_id, balance=0.0, total_earned=0.0, total_withdrawn=0.0)
            db.add(wallet)
            await db.flush()
        balance_before = wallet.balance or 0
        wallet.balance = balance_before + total_commission
        wallet.total_earned = (wallet.total_earned or 0) + total_commission
        txn = WalletTransaction(
            wallet_id=wallet.id,
            transaction_type="CREDIT",
            amount=total_commission,
            balance_before=balance_before,
            balance_after=wallet.balance,
            reference_id=str(booking.id),
            description=f"Commission for booking {booking.booking_number}. {payload.notes or ''}".strip(),
        )
        db.add(txn)

    # Mark booking CLOSED
    booking.status = BookingStatus.CLOSED
    settlement_note = f"Settled by {current_user.get('email', 'admin')}. Commission: ₹{total_commission:.2f}. {payload.notes or ''}".strip()
    await _add_status_log(db, booking.id, BookingStatus.CLOSED, current_user["user_id"], settlement_note)
    await db.commit()

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
    if booking.status not in [BookingStatus.PAID, BookingStatus.PAYMENT_PENDING, BookingStatus.COMPLETED, BookingStatus.INVOICE_GENERATED]:
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
    booking.status = BookingStatus.IN_PROGRESS
    await _add_status_log(db, booking.id, BookingStatus.IN_PROGRESS, current_user["user_id"],
                         f"Quotation approved by admin/CCO ({current_user.get('email', '')}). Work can now start.")
    await db.commit()
    return success_response(data={"status": booking.status.value}, message="Quotation approved — work can begin")
