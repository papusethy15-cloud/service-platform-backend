import asyncio
from app.core.background_tasks import track_task
from app.utils.timezone import now_ist, now_utc
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text as sa_text
from uuid import UUID
from app.core.database import get_db
from app.websocket.manager import publish_event, WSEvent, booking_room, ADMIN_BOOKINGS_ROOM
from app.api.deps import AdminCCOTech, AnyAuthenticated, AdminOnly
from app.api.v1.schemas.quotation import (
    AddQuotationPartRequest,
    AddQuotationServiceRequest,
    ApplyAdjustmentRequest,
    ApplyDiscountRequest,
    CreateQuotationRequest,
    QuotationActionRequest,
    UpdateQuotationPartRequest,
    UpdateQuotationRequest,
    VerifyCustomServiceRequest,
)
from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer
from app.models.quotation import (
    PartSource,
    Quotation,
    QuotationAppliance,
    QuotationPartItem,
    QuotationServiceItem,
    QuotationStatus,
    QuotationStatusLog,
)
from app.models.service import Service
from app.models.technician import Technician
from app.models.domain import Domain
from app.models.domain import ServiceCityPrice
from app.utils.response import success_response, iso
from app.utils.notify import push_to_technician

router = APIRouter()

EDITABLE_STATUSES = {QuotationStatus.DRAFT, QuotationStatus.REJECTED, QuotationStatus.REVISED}


def generate_quotation_number() -> str:
    return "QTN" + now_ist().strftime("%Y%m%d%H%M%S%f")[-12:]


async def _get_customer_id(db: AsyncSession, user_id: str):
    customer = (await db.execute(select(Customer).where(Customer.user_id == UUID(user_id)))).scalar_one_or_none()
    return customer.id if customer else None


async def _get_technician_id(db: AsyncSession, user_id: str):
    technician = (await db.execute(select(Technician).where(Technician.user_id == UUID(user_id)))).scalar_one_or_none()
    return technician.id if technician else None


async def _get_quotation_or_404(db: AsyncSession, quotation_id: UUID) -> Quotation:
    quotation = (await db.execute(select(Quotation).where(Quotation.id == quotation_id, Quotation.is_active == True))).scalar_one_or_none()
    if not quotation:
        raise HTTPException(status_code=404, detail="Quotation not found")
    return quotation


async def _ensure_access(db: AsyncSession, quotation: Quotation, current_user: dict):
    role = current_user["role"]
    if role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
        return
    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if role == "CUSTOMER":
        customer_id = await _get_customer_id(db, current_user["user_id"])
        if not customer_id or booking.customer_id != customer_id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif role == "TECHNICIAN":
        technician_id = await _get_technician_id(db, current_user["user_id"])
        if quotation.created_by != UUID(current_user["user_id"]) and booking.technician_id != technician_id:
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        raise HTTPException(status_code=403, detail="Access denied")


async def _add_status_log(db: AsyncSession, quotation: Quotation, user_id: str, notes: str | None = None):
    db.add(
        QuotationStatusLog(
            quotation_id=quotation.id,
            status=quotation.status,
            changed_by=UUID(user_id),
            notes=notes,
        )
    )


async def _recalculate_quotation(db: AsyncSession, quotation: Quotation):
    service_items = (
        await db.execute(select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == quotation.id, QuotationServiceItem.is_active == True))
    ).scalars().all()
    part_items = (
        await db.execute(select(QuotationPartItem).where(QuotationPartItem.quotation_id == quotation.id, QuotationPartItem.is_active == True))
    ).scalars().all()

    # Exclude repeat-complaint items from totals (they show on invoice as ₹0)
    quotation.services_total = sum(
        item.total_price for item in service_items
        if not getattr(item, 'is_repeat_complaint', False)
    )
    quotation.parts_total = sum(
        item.total_price for item in part_items
        if not getattr(item, 'is_repeat_complaint', False)
    )
    quotation.subtotal_amount = (
        quotation.labour_charges
        + quotation.service_charges
        + quotation.services_total
        + quotation.parts_total
    )

    # ── Dynamic coupon re-evaluation ──────────────────────────────────────────
    # Re-compute coupon_discount every time totals change so the discount always
    # reflects the CURRENT subtotal (not the subtotal at apply time, which was 0).
    coupon_disc = 0.0
    coupon_id = getattr(quotation, 'coupon_id', None)
    if coupon_id:
        from app.models.coupon import Coupon as CouponModel
        coupon_rec = (await db.execute(
            select(CouponModel).where(CouponModel.id == coupon_id, CouponModel.is_active == True)
        )).scalar_one_or_none()
        if coupon_rec:
            base_val = quotation.subtotal_amount or 0.0
            if coupon_rec.discount_type == 'PERCENTAGE':
                raw_disc = base_val * (coupon_rec.discount_value / 100.0)
            else:  # FLAT
                raw_disc = coupon_rec.discount_value
            if coupon_rec.max_discount_amount:
                raw_disc = min(raw_disc, coupon_rec.max_discount_amount)
            coupon_disc = round(raw_disc)
        quotation.coupon_discount = coupon_disc
    else:
        coupon_disc = getattr(quotation, "coupon_discount", 0.0) or 0.0
    # ─────────────────────────────────────────────────────────────────────────

    taxable_amount = max(quotation.subtotal_amount - quotation.discount_amount - coupon_disc + quotation.adjustment_amount, 0.0)
    # Tax mode: NONE = zero tax regardless of tax_percent; B2C/B2B = apply tax_percent
    tax_mode = getattr(quotation, 'tax_mode', 'B2C') or 'B2C'
    if tax_mode == 'NONE':
        quotation.tax_amount = 0.0
        quotation.tax_percent = 0.0
    else:
        quotation.tax_amount = round(taxable_amount * (quotation.tax_percent / 100.0))
    quotation.total_amount = round(taxable_amount + quotation.tax_amount)


def _quotation_summary(quotation: Quotation, booking_number: str | None = None):
    return {
        "id": str(quotation.id),
        "quotation_number": quotation.quotation_number,
        "booking_id": str(quotation.booking_id),
        "booking_number": booking_number,
        "version": quotation.version,
        "status": quotation.status.value,
        "labour_charges": quotation.labour_charges,
        "service_charges": quotation.service_charges,
        "services_total": quotation.services_total,
        "parts_total": quotation.parts_total,
        "discount_amount": quotation.discount_amount,
        "adjustment_amount": quotation.adjustment_amount,
        "subtotal_amount": quotation.subtotal_amount,
        "tax_percent": quotation.tax_percent,
        "tax_amount": quotation.tax_amount,
        "total_amount": quotation.total_amount,
        "remarks": quotation.remarks,
        "approved_at": iso(quotation.approved_at) if quotation.approved_at else None,
        "rejection_reason": quotation.rejection_reason,
        "created_at": iso(quotation.created_at),
        "tax_mode": getattr(quotation, 'tax_mode', 'B2C') or 'B2C',
        "customer_gst_number":  getattr(quotation, 'customer_gst_number', None),
        "customer_gst_name":    getattr(quotation, 'customer_gst_name', None),
        "customer_gst_address": getattr(quotation, 'customer_gst_address', None),
        "coupon_code":          getattr(quotation, 'coupon_code', None),
        "coupon_discount":      getattr(quotation, 'coupon_discount', 0.0) or 0.0,
    }


def _broadcast_quotation(quotation: Quotation, event_type: str, actor_user_id: str | None = None, booking_number: str | None = None):
    """
    Fire-and-forget WS broadcast so the admin dashboard and the captain app
    stay in sync in real time when either side edits a quotation.
    Broadcasts to the booking room (subscribed by admin's booking modal +
    the technician's booking-level connection) AND the global admin
    bookings room (admin list views).
    """
    payload = _quotation_summary(quotation, booking_number=booking_number)
    payload["actor_user_id"] = actor_user_id
    room = booking_room(str(quotation.booking_id))
    track_task(publish_event(room, event_type, payload))
    track_task(publish_event(ADMIN_BOOKINGS_ROOM, event_type, payload))


@router.post("", summary="Create quotation")
async def create_quotation(
    payload: CreateQuotationRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    booking = (await db.execute(select(Booking).where(Booking.id == UUID(payload.booking_id)))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # ── Coupon: only eligible on the 1st quotation for this booking ───────────
    # Store coupon reference at creation time. _recalculate_quotation will compute
    # the actual discount dynamically as services are added, so discount is always correct.
    # We pull coupon_code from payload OR from booking.coupon_code (whichever is available).
    coupon_id_q = None
    coupon_code_q = None
    coupon_discount_q = 0.0
    raw_coupon_code = (getattr(payload, 'coupon_code', None) or '').strip().upper() or                       (getattr(booking, 'coupon_code', None) or '').strip().upper()
    if raw_coupon_code:
        existing_count = (await db.execute(
            select(func.count()).select_from(
                select(Quotation).where(Quotation.booking_id == booking.id, Quotation.is_active == True).subquery()
            )
        )).scalar_one()
        if existing_count == 0:
            from app.models.coupon import Coupon
            coupon_q = (await db.execute(
                select(Coupon).where(Coupon.code == raw_coupon_code, Coupon.is_active == True)
            )).scalar_one_or_none()
            if coupon_q:
                # Store reference — _recalculate_quotation computes discount on current subtotal
                coupon_id_q = coupon_q.id
                coupon_code_q = coupon_q.code
                # coupon_discount_q stays 0.0 — recalc will set it correctly after services added
                # Sync used_count to real booking usage (fixes any inflation from old code)
                from app.models.booking import Booking as _BkSync
                correct_count = (await db.execute(
                    select(func.count()).select_from(
                        select(_BkSync).where(
                            _BkSync.coupon_code == coupon_q.code,
                            _BkSync.is_active == True,
                        ).subquery()
                    )
                )).scalar_one() or 0
                if coupon_q.used_count != correct_count:
                    coupon_q.used_count = correct_count

    quotation = Quotation(
        quotation_number=generate_quotation_number(),
        booking_id=booking.id,
        created_by=UUID(current_user["user_id"]),
        labour_charges=payload.labour_charges,
        service_charges=payload.service_charges,
        tax_percent=payload.tax_percent if payload.tax_mode != 'NONE' else 0.0,
        tax_mode=payload.tax_mode or 'B2C',
        customer_gst_number=payload.customer_gst_number if payload.tax_mode == 'B2B' else None,
        customer_gst_name=payload.customer_gst_name if payload.tax_mode == 'B2B' else None,
        customer_gst_address=payload.customer_gst_address if payload.tax_mode == 'B2B' else None,
        remarks=payload.remarks,
        coupon_id=coupon_id_q,
        coupon_code=coupon_code_q,
        coupon_discount=coupon_discount_q,
    )
    db.add(quotation)
    await db.flush()
    await _recalculate_quotation(db, quotation)
    await _add_status_log(db, quotation, current_user["user_id"], "Quotation created")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_CREATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Quotation created successfully")


@router.get("", summary="Quotation list")
async def list_quotations(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    booking_id: str = Query(None),
    status: str = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    query = select(Quotation).where(Quotation.is_active == True)
    if booking_id:
        query = query.where(Quotation.booking_id == UUID(booking_id))
    if status:
        query = query.where(Quotation.status == QuotationStatus(status))

    role = current_user["role"]
    if role == "CUSTOMER":
        customer_id = await _get_customer_id(db, current_user["user_id"])
        query = query.join(Booking, Booking.id == Quotation.booking_id).where(Booking.customer_id == customer_id)
    elif role == "TECHNICIAN":
        technician_id = await _get_technician_id(db, current_user["user_id"])
        query = query.join(Booking, Booking.id == Quotation.booking_id).where(
            or_(Quotation.created_by == UUID(current_user["user_id"]), Booking.technician_id == technician_id)
        )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    quotations = (await db.execute(query.order_by(Quotation.created_at.desc()).offset((page - 1) * per_page).limit(per_page))).scalars().all()
    # Bulk load booking info (booking_number, customer_id, domain_id)
    booking_ids = list({q.booking_id for q in quotations})
    booking_map: dict = {}      # booking_id -> booking_number
    customer_id_map: dict = {}  # booking_id -> customer_id
    domain_id_map: dict = {}    # booking_id -> domain_id
    if booking_ids:
        bookings_q = (await db.execute(
            select(Booking.id, Booking.booking_number, Booking.customer_id, Booking.domain_id)
            .where(Booking.id.in_(booking_ids))
        )).all()
        for b in bookings_q:
            booking_map[str(b.id)] = b.booking_number
            customer_id_map[str(b.id)] = b.customer_id
            domain_id_map[str(b.id)] = b.domain_id
    # Bulk load customer names
    customer_ids = list({v for v in customer_id_map.values() if v})
    cust_name_map: dict = {}   # customer_id -> name
    cust_mobile_map: dict = {} # customer_id -> mobile
    if customer_ids:
        custs_q = (await db.execute(
            select(Customer.id, Customer.name, Customer.mobile).where(Customer.id.in_(customer_ids))
        )).all()
        for c in custs_q:
            cust_name_map[str(c.id)] = c.name
            cust_mobile_map[str(c.id)] = c.mobile
    # Bulk load domain names
    domain_ids = list({v for v in domain_id_map.values() if v})
    domain_name_map: dict = {}  # domain_id -> name
    if domain_ids:
        domains_q = (await db.execute(
            select(Domain.id, Domain.name).where(Domain.id.in_(domain_ids))
        )).all()
        for d in domains_q:
            domain_name_map[str(d.id)] = d.name

    def _enrich(q: Quotation) -> dict:
        summary = _quotation_summary(q, booking_map.get(str(q.booking_id)))
        bid = str(q.booking_id)
        cid = str(customer_id_map.get(bid) or '')
        did = str(domain_id_map.get(bid) or '')
        summary["customer_name"]   = cust_name_map.get(cid)
        summary["customer_mobile"] = cust_mobile_map.get(cid)
        summary["domain_name"]     = domain_name_map.get(did)
        return summary

    return success_response(
        data={
            "items": [_enrich(q) for q in quotations],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
    )


@router.get("/{quotation_id}", summary="Quotation details")
async def get_quotation(
    quotation_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    await _ensure_access(db, quotation, current_user)
    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    booking_number = booking.booking_number if booking else None
    # Fetch customer info to expose in quotation detail
    from app.models.customer import Customer as _Customer
    customer = None
    if booking and booking.customer_id:
        customer = (await db.execute(select(_Customer).where(_Customer.id == booking.customer_id))).scalar_one_or_none()
    service_items = (
        await db.execute(select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == quotation.id, QuotationServiceItem.is_active == True))
    ).scalars().all()
    part_items = (
        await db.execute(select(QuotationPartItem).where(QuotationPartItem.quotation_id == quotation.id, QuotationPartItem.is_active == True))
    ).scalars().all()
    data = _quotation_summary(quotation, booking_number)
    data["customer_id"] = str(booking.customer_id) if booking and booking.customer_id else None
    data["customer_name"] = customer.name if customer else None
    data["booking_city"] = booking.address_city if booking and hasattr(booking, "address_city") else None
    data["booking_appliance_brand"] = booking.appliance_brand if booking else None
    data["booking_appliance_model"] = booking.appliance_model if booking else None
    # ── service_category_id: used by frontends to filter appliances by category ──
    _bkg_svc_cat_id = None
    if booking and booking.service_id:
        try:
            from app.models.service import Service as _SvcModel
            _bkg_svc = (await db.execute(
                select(_SvcModel).where(_SvcModel.id == booking.service_id)
            )).scalar_one_or_none()
            if _bkg_svc:
                _bkg_svc_cat_id = str(_bkg_svc.category_id) if _bkg_svc.category_id else None
        except Exception:
            pass
    data["service_category_id"] = _bkg_svc_cat_id
    data["services"] = [
        {
            "id": str(item.id),
            "service_id": str(item.service_id) if item.service_id else None,
            "is_pending_verify": getattr(item, "is_pending_verify", 0) or 0,
            "custom_service_name": getattr(item, "custom_service_name", None),
            "service_name": item.service_name,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "total_price": item.total_price,
            "appliance_label": getattr(item, "appliance_label", None),
            "is_repeat_complaint": bool(getattr(item, "is_repeat_complaint", False)),
        }
        for item in service_items
    ]
    def _decode_part(item):
        notes_raw = item.notes or ""
        appliance_label = None
        display_notes = notes_raw
        if notes_raw.startswith("appliance:"):
            remainder = notes_raw[len("appliance:"):]
            if "|" in remainder:
                appliance_label, display_notes = remainder.split("|", 1)
            else:
                appliance_label = remainder
                display_notes = ""
        return {
            "id": str(item.id),
            "part_name": item.part_name,
            "part_source": item.part_source.value,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "purchase_price": getattr(item, "purchase_price", 0) or 0,
            "total_price": item.total_price,
            "vendor_name": item.vendor_name,
            "bill_number": item.bill_number,
            "notes": display_notes or None,
            "appliance_label": appliance_label,
            "inventory_item_id": str(item.inventory_item_id) if getattr(item, "inventory_item_id", None) else None,
            "is_pending_verify": getattr(item, "is_pending_verify", 0) or 0,
        }
    data["parts"] = [_decode_part(item) for item in part_items]

    # Fetch quotation_appliances using raw SQL to avoid ORM column-mapping issues
    # (e.g. timezone mismatch on created_at between DB and SQLAlchemy model).
    try:
        appliance_result = (await db.execute(
            sa_text(
                "SELECT id, appliance_id, appliance_label "
                "FROM quotation_appliances "
                "WHERE quotation_id = :qid AND is_active = true "
                "ORDER BY created_at ASC"
            ),
            {"qid": str(quotation.id)}
        )).mappings().all()
        data["appliances"] = [
            {
                "id": str(row["id"]),
                "appliance_id": str(row["appliance_id"]) if row["appliance_id"] else None,
                "appliance_label": row["appliance_label"],
            }
            for row in appliance_result
        ]
    except Exception as _appliance_exc:
        import logging as _log
        _log.getLogger(__name__).error("Failed to fetch quotation_appliances for %s: %s", quotation.id, _appliance_exc)
        data["appliances"] = []
    return success_response(data=data)


@router.put("/{quotation_id}", summary="Update quotation")
async def update_quotation(
    quotation_id: UUID,
    payload: UpdateQuotationRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Only editable quotations can be updated")
    data = payload.model_dump(exclude_none=True)
    # Handle tax_mode specially
    tax_mode = data.pop('tax_mode', None)
    if tax_mode is not None:
        quotation.tax_mode = tax_mode
        if tax_mode == 'NONE':
            quotation.tax_percent = 0.0
            quotation.customer_gst_number = None
            quotation.customer_gst_name = None
            quotation.customer_gst_address = None
        elif tax_mode == 'B2C':
            quotation.customer_gst_number = None
            quotation.customer_gst_name = None
            quotation.customer_gst_address = None
        # B2B: GST fields are set below via data dict
    # Don't update tax_percent if mode is NONE
    if 'tax_percent' in data and getattr(quotation, 'tax_mode', 'B2C') == 'NONE':
        data.pop('tax_percent')
    for field, value in data.items():
        setattr(quotation, field, value)
    await _recalculate_quotation(db, quotation)
    await _add_status_log(db, quotation, current_user["user_id"], "Quotation updated")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Quotation updated successfully")


@router.delete("/{quotation_id}", summary="Delete quotation")
async def delete_quotation(
    quotation_id: UUID,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status == QuotationStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Approved quotation cannot be deleted")
    quotation.is_active = False
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_DELETED, current_user["user_id"])
    return success_response(message="Quotation deleted successfully")




@router.post("/{quotation_id}/revert-to-draft", summary="Revert submitted/approved quotation back to DRAFT for editing")
async def revert_quotation_to_draft(
    quotation_id: UUID,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    allowed = {QuotationStatus.SUBMITTED, QuotationStatus.APPROVED, QuotationStatus.REJECTED}
    if quotation.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot revert quotation in status {quotation.status.value} to draft")
    quotation.status = QuotationStatus.DRAFT
    quotation.submitted_at = None
    quotation.approved_at = None
    quotation.approved_by = None
    await _add_status_log(db, quotation, current_user["user_id"], "Reverted to DRAFT for editing")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Quotation reverted to DRAFT")

@router.post("/{quotation_id}/submit", summary="Submit quotation")
async def submit_quotation(
    quotation_id: UUID,
    payload: Optional[QuotationActionRequest] = None,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not in a submittable state")
    quotation.status = QuotationStatus.SUBMITTED
    quotation.submitted_at = now_utc().replace(tzinfo=None)  # naive UTC for TIMESTAMP WITHOUT TIME ZONE column
    notes = (payload.notes if payload and payload.notes else None) or "Quotation submitted"
    await _add_status_log(db, quotation, current_user["user_id"], notes)
    # Explicit booking query — async sessions don't support lazy relationship loading
    _submit_booking = (await db.execute(
        select(Booking).where(Booking.id == quotation.booking_id)
    )).scalar_one_or_none()
    _submit_bnum = _submit_booking.booking_number if _submit_booking else ""
    _submit_customer_id = _submit_booking.customer_id if _submit_booking else None
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    # Also fire dedicated QUOTATION_SUBMITTED event so admin notification system can alert
    from app.websocket.manager import publish_event as _pub, ADMIN_BOOKINGS_ROOM as _ABR
    track_task(_pub(_ABR, WSEvent.QUOTATION_SUBMITTED, {
        "quotation_id": str(quotation.id),
        "booking_id":   str(quotation.booking_id),
        "booking_number": _submit_bnum,
        "submitted_by": current_user.get("name", "Technician"),
    }))
    # FCM push to customer so they are notified even with screen closed
    # NOTE: fcm_token is stored on the Customer model (not User), so we use _cust.fcm_token.
    #       We still load the User row to get the user_id for the Notification record.
    if _submit_customer_id:
        try:
            from app.models.customer import Customer
            from app.utils.fcm import send_simple_push
            _cust = (await db.execute(
                select(Customer).where(Customer.id == _submit_customer_id)
            )).scalar_one_or_none()
            if _cust:
                from app.models.notification import Notification
                # Save in-app notification record (requires user_id)
                if _cust.user_id:
                    from app.models.user import User
                    _cust_user = (await db.execute(
                        select(User).where(User.id == _cust.user_id)
                    )).scalar_one_or_none()
                    if _cust_user:
                        _notif = Notification(
                            user_id=_cust_user.id,
                            title="Quotation Ready for Review 📋",
                            body=f"Your technician has submitted a quotation for booking {_submit_bnum}. Tap to review and approve.",
                            channel="PUSH",
                            data={"type": "QUOTATION_SUBMITTED", "booking_id": str(quotation.booking_id), "quotation_id": str(quotation.id)},
                            is_read=False,
                        )
                        db.add(_notif)
                        await db.commit()
                # FCM push — token is on Customer model, not User
                if _cust.fcm_token:
                    track_task(send_simple_push(
                        fcm_token=_cust.fcm_token,
                        title="Quotation Ready for Review 📋",
                        body=f"Your technician has submitted a quotation for booking {_submit_bnum}. Tap to review and approve.",
                        data={"type": "QUOTATION_SUBMITTED", "booking_id": str(quotation.booking_id), "quotation_id": str(quotation.id)},
                    ))
        except Exception as _ce:
            import logging; logging.getLogger(__name__).warning(f"Customer FCM on submit failed: {_ce}")
    # FCM push to all Admin/CCO users so they get notified even when the dashboard is closed
    try:
        from app.models.user import User as _AdminUser
        from app.utils.fcm import send_simple_push as _push
        _admin_users = (await db.execute(
            select(_AdminUser).where(
                _AdminUser.role.in_(["SUPER_ADMIN", "ADMIN", "CCO"]),
                _AdminUser.fcm_token.isnot(None),
                _AdminUser.is_active == True,
            )
        )).scalars().all()
        for _au in _admin_users:
            track_task(_push(
                fcm_token=_au.fcm_token,
                title="New Quotation Submitted \U0001f4cb",
                body=f"Booking {_submit_bnum} — technician has submitted a quotation for approval.",
                data={"type": "QUOTATION_SUBMITTED", "booking_id": str(quotation.booking_id), "quotation_id": str(quotation.id), "booking_number": _submit_bnum},
            ))
    except Exception as _ae:
        import logging; logging.getLogger(__name__).warning(f"Admin/CCO FCM on submit failed: {_ae}")
    return success_response(data=_quotation_summary(quotation), message="Quotation submitted successfully")


@router.post("/{quotation_id}/approve", summary="Approve quotation")
async def approve_quotation(
    quotation_id: UUID,
    payload: QuotationActionRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    await _ensure_access(db, quotation, current_user)
    if current_user["role"] not in {"SUPER_ADMIN", "ADMIN", "CCO", "CUSTOMER"}:
        raise HTTPException(status_code=403, detail="Access denied")
    # Quotation must be in SUBMITTED status to be approved.
    # DRAFT quotations must be submitted first (via /submit) before they can be approved.
    # ADMIN/CCO/SUPER_ADMIN can also directly approve a DRAFT quotation they created on behalf.
    approvable_statuses = {QuotationStatus.SUBMITTED, QuotationStatus.DRAFT, QuotationStatus.REJECTED}
    if quotation.status not in approvable_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve quotation in status '{quotation.status.value}'. Only SUBMITTED, DRAFT or REJECTED quotations can be approved."
        )
    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    # A quotation cannot be approved until a technician is assigned to the booking —
    # approval kicks off the repair workflow (inspection/work/invoice), which assumes
    # a technician is on the job.
    if booking and not booking.technician_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot approve quotation: no technician assigned to this booking yet. Assign a technician first."
        )
    quotation.status = QuotationStatus.APPROVED
    quotation.approved_at = now_utc().replace(tzinfo=None)  # naive UTC for TIMESTAMP WITHOUT TIME ZONE column
    quotation.approved_by = UUID(current_user["user_id"])
    _PRE_WORK_STATUSES = {
        BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.ASSIGNED,
        BookingStatus.ACCEPTED, BookingStatus.TECHNICIAN_ACCEPTED, BookingStatus.EN_ROUTE,
        BookingStatus.ARRIVED, BookingStatus.INSPECTING, BookingStatus.IN_PROGRESS,
    }
    if booking:
        booking.base_amount = round(quotation.subtotal_amount - quotation.discount_amount + quotation.adjustment_amount, 2)
        booking.gst_amount = quotation.tax_amount
        booking.total_amount = quotation.total_amount
        # Bug 7 fix: advance booking status so it's filterable and reflects approval
        if booking.status in _PRE_WORK_STATUSES:
            booking.status = BookingStatus.QUOTATION_APPROVED
            # Write a BookingStatusLog entry so the CCO/admin timeline shows this transition
            from app.models.booking import BookingStatusLog as _BookingStatusLog
            db.add(_BookingStatusLog(
                booking_id=booking.id,
                status=BookingStatus.QUOTATION_APPROVED,
                changed_by=UUID(current_user["user_id"]),
                notes=f"Quotation {quotation.quotation_number} approved — booking ready for repair",
            ))
    await _add_status_log(db, quotation, current_user["user_id"], payload.notes or "Quotation approved")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    # Bug 6 fix: also broadcast a dedicated QUOTATION_APPROVED event so captain app
    # can show an in-app snackbar/banner even if FCM is delayed.
    _broadcast_quotation(quotation, WSEvent.QUOTATION_APPROVED, current_user["user_id"])
    # ── Notify assigned technician ────────────────────────────────────────
    if booking and booking.technician_id:
        try:
            tech = (await db.execute(select(Technician).where(Technician.id == booking.technician_id))).scalar_one_or_none()
            if tech:
                bnum = booking.booking_number if booking else str(quotation.booking_id)[:8]
                await push_to_technician(
                    db=db, technician=tech,
                    title="Quotation Approved ✅",
                    body=f"Admin approved your quotation for booking {bnum}. You can now proceed with the work.",
                    notif_type="BOOKING",
                    data={"type": "QUOTATION_APPROVED", "booking_id": str(quotation.booking_id), "quotation_id": str(quotation.id)},
                )
        except Exception as _e:
            import logging; logging.getLogger(__name__).warning(f"Quotation approve notify failed: {_e}")
    return success_response(data=_quotation_summary(quotation), message="Quotation approved successfully")


@router.post("/{quotation_id}/reject", summary="Reject quotation")
async def reject_quotation(
    quotation_id: UUID,
    payload: QuotationActionRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    await _ensure_access(db, quotation, current_user)
    if current_user["role"] not in {"SUPER_ADMIN", "ADMIN", "CCO", "CUSTOMER"}:
        raise HTTPException(status_code=403, detail="Access denied")
    quotation.status = QuotationStatus.REJECTED
    quotation.rejection_reason = payload.reason or payload.notes
    await _add_status_log(db, quotation, current_user["user_id"], payload.reason or "Quotation rejected")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Quotation rejected successfully")


@router.post("/{quotation_id}/revise", summary="Revise quotation")
async def revise_quotation(
    quotation_id: UUID,
    payload: QuotationActionRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    root_id = quotation.original_quotation_id or quotation.id
    version_query = select(func.max(Quotation.version)).where(
        or_(Quotation.id == root_id, Quotation.original_quotation_id == root_id)
    )
    next_version = ((await db.execute(version_query)).scalar_one() or quotation.version) + 1

    revised = Quotation(
        quotation_number=generate_quotation_number(),
        booking_id=quotation.booking_id,
        created_by=UUID(current_user["user_id"]),
        original_quotation_id=root_id,
        version=next_version,
        status=QuotationStatus.DRAFT,
        labour_charges=quotation.labour_charges,
        service_charges=quotation.service_charges,
        discount_amount=quotation.discount_amount,
        adjustment_amount=quotation.adjustment_amount,
        tax_percent=quotation.tax_percent,
        remarks=payload.notes or quotation.remarks,
    )
    db.add(revised)
    await db.flush()

    service_items = (
        await db.execute(select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == quotation.id, QuotationServiceItem.is_active == True))
    ).scalars().all()
    for item in service_items:
        db.add(
            QuotationServiceItem(
                quotation_id=revised.id,
                service_id=item.service_id,
                service_name=item.service_name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,
            )
        )

    part_items = (
        await db.execute(select(QuotationPartItem).where(QuotationPartItem.quotation_id == quotation.id, QuotationPartItem.is_active == True))
    ).scalars().all()
    for item in part_items:
        db.add(
            QuotationPartItem(
                quotation_id=revised.id,
                part_name=item.part_name,
                part_source=item.part_source,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,
                vendor_name=item.vendor_name,
                bill_number=item.bill_number,
                notes=item.notes,
            )
        )

    quotation.status = QuotationStatus.REVISED
    await _recalculate_quotation(db, revised)
    await _add_status_log(db, quotation, current_user["user_id"], "Quotation revised")
    await _add_status_log(db, revised, current_user["user_id"], "New quotation revision created")
    await db.commit()
    _broadcast_quotation(revised, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(revised), message="Quotation revised successfully")


@router.get("/{quotation_id}/history", summary="Revision history")
async def quotation_history(
    quotation_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    await _ensure_access(db, quotation, current_user)
    root_id = quotation.original_quotation_id or quotation.id
    versions = (
        await db.execute(
            select(Quotation).where(or_(Quotation.id == root_id, Quotation.original_quotation_id == root_id)).order_by(Quotation.version)
        )
    ).scalars().all()
    version_ids = [item.id for item in versions]
    logs = (
        await db.execute(
            select(QuotationStatusLog).where(QuotationStatusLog.quotation_id.in_(version_ids)).order_by(QuotationStatusLog.created_at)
        )
    ).scalars().all()
    return success_response(
        data={
            "versions": [_quotation_summary(item) for item in versions],
            "events": [
                {
                    "quotation_id": str(item.quotation_id),
                    "status": item.status.value,
                    "notes": item.notes,
                    "created_at": iso(item.created_at),
                }
                for item in logs
            ],
        }
    )


@router.post("/{quotation_id}/services", summary="Add service (existing catalogue or new custom service by technician)")
async def add_service_to_quotation(
    quotation_id: UUID,
    payload: AddQuotationServiceRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")

    # ── PATH A: Existing catalogue service ───────────────────────────────────
    if payload.service_id:
        service = (await db.execute(
            select(Service).where(Service.id == UUID(payload.service_id), Service.is_active == True)
        )).scalar_one_or_none()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        # ── Resolve city-overridden price from booking.city_id ─────────────────
        booking_for_city = (await db.execute(
            select(Booking).where(Booking.id == quotation.booking_id)
        )).scalar_one_or_none()
        resolved_city_price: float | None = None
        if booking_for_city and booking_for_city.city_id and payload.unit_price is None:
            city_price_row = (await db.execute(
                select(ServiceCityPrice).where(
                    ServiceCityPrice.service_id == service.id,
                    ServiceCityPrice.city_id == booking_for_city.city_id,
                    ServiceCityPrice.is_active == True,
                )
            )).scalar_one_or_none()
            if city_price_row:
                resolved_city_price = city_price_row.price
        unit_price = payload.unit_price if payload.unit_price is not None else (resolved_city_price if resolved_city_price is not None else service.base_price)
        encoded_name = f"{payload.appliance_label} :: {service.name}" if payload.appliance_label else service.name
        item = QuotationServiceItem(
            quotation_id=quotation.id,
            service_id=service.id,
            service_name=encoded_name,
            quantity=payload.quantity,
            unit_price=unit_price,
            total_price=round(unit_price * payload.quantity),
            appliance_label=payload.appliance_label,
            is_pending_verify=0,
        )
        db.add(item)
        await db.flush()
        await _recalculate_quotation(db, quotation)
        await db.commit()
        _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
        return success_response(
            data={"id": str(item.id), "total_price": item.total_price, "is_pending_verify": 0},
            message="Service added successfully"
        )

    # ── PATH B: Custom / new service suggested by technician ─────────────────
    custom_name = (payload.custom_service_name or "").strip()
    if not custom_name:
        raise HTTPException(status_code=400, detail="Provide service_id for existing service, or custom_service_name for a new service")

    unit_price = payload.custom_base_price or payload.unit_price or 0.0

    # Create a placeholder Service record (inactive until admin verifies)
    placeholder = Service(
        # Use a temporary category — admin will reassign during verification.
        # Find any existing category to satisfy the FK constraint.
        category_id=(await db.execute(select(Service.category_id).limit(1))).scalar_one_or_none(),
        name=custom_name,
        description=f"Suggested by technician (pending admin verification)",
        base_price=unit_price,
        gst_percent=18.0,
        duration_mins=60,
        is_visible=False,       # not visible to customers yet
        is_active=False,        # excluded from search results
        is_pending_verify=1,    # flags this for admin review queue
        suggested_by_tech=UUID(current_user["user_id"]),
    )
    # If no category exists at all, create a fallback one
    if placeholder.category_id is None:
        fallback_cat = ServiceCategory(name="General", description="Auto-created", sort_order=99)
        db.add(fallback_cat)
        await db.flush()
        placeholder.category_id = fallback_cat.id

    db.add(placeholder)
    await db.flush()

    encoded_name = f"{payload.appliance_label} :: {custom_name}" if payload.appliance_label else custom_name
    item = QuotationServiceItem(
        quotation_id=quotation.id,
        service_id=placeholder.id,      # link to placeholder so admin can find it
        service_name=encoded_name,
        quantity=payload.quantity,
        unit_price=unit_price,
        total_price=round(unit_price * payload.quantity),
        appliance_label=payload.appliance_label,
        is_pending_verify=1,            # pending admin verify
        custom_service_name=custom_name,
    )
    db.add(item)
    await db.flush()
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(
        data={"id": str(item.id), "total_price": item.total_price, "is_pending_verify": 1,
              "service_id": str(placeholder.id)},
        message=f"Custom service '{custom_name}' added and submitted for admin verification"
    )


@router.post("/{quotation_id}/services/{service_item_id}/verify",
             summary="Admin: verify and promote a tech-suggested custom service to the catalogue [Admin]")
async def verify_custom_service(
    quotation_id: UUID,
    service_item_id: UUID,
    payload: VerifyCustomServiceRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin flow for verifying a tech-suggested service:
    1. Finds the QuotationServiceItem (must have is_pending_verify=1)
    2. Updates/promotes the placeholder Service to a real catalogue entry
    3. Optionally sets a commission rule in the technician's commission group
    4. Marks the quotation item as is_pending_verify=2 (verified)
    """
    quotation = await _get_quotation_or_404(db, quotation_id)
    item = (await db.execute(
        select(QuotationServiceItem).where(
            QuotationServiceItem.id == service_item_id,
            QuotationServiceItem.quotation_id == quotation_id,
        )
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Service item not found")
    if item.is_pending_verify != 1:
        raise HTTPException(status_code=400, detail="This service item is not pending verification")

    # Find the placeholder Service record
    placeholder = (await db.execute(
        select(Service).where(Service.id == item.service_id)
    )).scalar_one_or_none() if item.service_id else None

    # Validate category
    from app.models.service import ServiceCategory
    cat = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id == UUID(payload.category_id))
    )).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    if placeholder and placeholder.is_pending_verify == 1:
        # Promote the placeholder to a real service
        placeholder.name          = payload.name
        placeholder.category_id   = cat.id
        placeholder.base_price    = payload.base_price
        placeholder.gst_percent   = payload.gst_percent
        placeholder.duration_mins = payload.duration_mins
        placeholder.is_visible    = payload.is_visible
        placeholder.is_active     = True          # now live in catalogue
        placeholder.is_pending_verify = 2         # verified
        real_service = placeholder

        # Link to domain if provided
        if payload.domain_id:
            from app.models.domain import DomainService, DomainCategory
            from app.api.v1.routes.services import _link_service_to_domain, _link_category_to_domain
            await _link_service_to_domain(db, real_service.id, UUID(payload.domain_id))
            await _link_category_to_domain(db, cat.id, UUID(payload.domain_id))
    else:
        raise HTTPException(status_code=400, detail="Placeholder service not found or already verified")

    # Mark the quotation item as verified
    item.is_pending_verify = 2
    item.service_id = real_service.id

    # Optionally add commission rule to the technician's group
    if payload.commission_type and payload.commission_value is not None:
        from app.models.commission import CommissionGroup, CommissionGroupAssignment, CommissionGroupRule
        from app.models.booking import Booking
        from app.models.technician import Technician

        booking = (await db.execute(
            select(Booking).where(Booking.id == quotation.booking_id)
        )).scalar_one_or_none()
        if booking and booking.technician_id:
            # Find this technician's commission group assignment
            assignment = (await db.execute(
                select(CommissionGroupAssignment).where(
                    CommissionGroupAssignment.technician_id == booking.technician_id
                )
            )).scalar_one_or_none()
            if assignment:
                existing_rule = (await db.execute(
                    select(CommissionGroupRule).where(
                        CommissionGroupRule.group_id == assignment.group_id,
                        CommissionGroupRule.service_id == real_service.id,
                    )
                )).scalar_one_or_none()
                if not existing_rule:
                    db.add(CommissionGroupRule(
                        group_id=assignment.group_id,
                        service_id=real_service.id,
                        commission_type=payload.commission_type,
                        rate=payload.commission_value,
                    ))

    # Store commission override on the item itself as a fallback
    if payload.commission_type and payload.commission_value is not None:
        item.tech_commission_override = payload.commission_value

    await db.flush()
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(
        data={
            "service_id": str(real_service.id),
            "service_name": real_service.name,
            "base_price": real_service.base_price,
            "is_pending_verify": 2,
        },
        message=f"Service '{real_service.name}' verified and added to catalogue"
    )


@router.post("/{quotation_id}/parts", summary="Add spare part")
async def add_part_to_quotation(
    quotation_id: UUID,
    payload: AddQuotationPartRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.inventory import InventoryItem, TechnicianStock, TechnicianStockLog, StockMovement, MovementType, TechnicianStockStatus, WarehouseStock
    from app.models.booking import Booking
    from app.models.technician import Technician

    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")

    # Encode appliance_label in notes: "appliance:Label|original_notes"
    notes_str = payload.notes or ""
    if payload.appliance_label:
        notes_str = f"appliance:{payload.appliance_label}|{notes_str}" if notes_str else f"appliance:{payload.appliance_label}"

    inventory_item_id = None
    is_pending_verify = 0

    # ── OFFICE STOCK: deduct from technician's stock ──────────────────────────
    if payload.part_source == "OFFICE_STOCK" and payload.inventory_item_id:
        inv_item = (await db.execute(
            select(InventoryItem).where(InventoryItem.id == UUID(payload.inventory_item_id), InventoryItem.is_active == True)
        )).scalar_one_or_none()
        if not inv_item:
            raise HTTPException(status_code=404, detail="Inventory item not found")

        # Find technician from booking
        booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
        technician_id = booking.technician_id if booking else None

        if technician_id:
            tech_stock = (await db.execute(
                select(TechnicianStock).where(
                    TechnicianStock.technician_id == technician_id,
                    TechnicianStock.item_id == inv_item.id,
                )
            )).scalar_one_or_none()

            if not tech_stock or tech_stock.quantity < payload.quantity:
                avail = tech_stock.quantity if tech_stock else 0
                raise HTTPException(status_code=400, detail=f"Insufficient technician stock. Available: {avail} {inv_item.unit}")

            tech_stock.quantity -= payload.quantity
            tech_stock.consumed_qty = (tech_stock.consumed_qty or 0) + payload.quantity
            inv_item.reserved_stock = max(0, (inv_item.reserved_stock or 0) - payload.quantity)

            db.add(StockMovement(
                item_id=inv_item.id,
                movement_type=MovementType.CONSUMPTION.value,
                quantity=-payload.quantity,
                technician_id=technician_id,
                booking_id=booking.id if booking else None,
                notes=f"Consumed in quotation {quotation.quotation_number}",
                performed_by=UUID(current_user["user_id"]),
            ))
            db.add(TechnicianStockLog(
                technician_id=technician_id,
                item_id=inv_item.id,
                booking_id=booking.id if booking else None,
                status=TechnicianStockStatus.CONSUMED.value,
                quantity=payload.quantity,
                notes=f"Consumed in quotation {quotation.quotation_number}",
                performed_by=UUID(current_user["user_id"]),
            ))
        inventory_item_id = inv_item.id
        # Auto-set sale price from catalogue if not provided
        if not payload.unit_price:
            payload = payload.model_copy(update={"unit_price": inv_item.selling_price or 0})

    # ── NEW PART: add to inventory catalogue as pending-verify ────────────────
    elif payload.is_new_part and payload.part_source == "MARKET_PURCHASE":
        existing = (await db.execute(
            select(InventoryItem).where(
                InventoryItem.name.ilike(payload.part_name.strip()),
                InventoryItem.is_active == True,
            )
        )).scalar_one_or_none()
        if not existing:
            new_item = InventoryItem(
                name=payload.part_name.strip(),
                cost_price=payload.purchase_price or 0,
                selling_price=payload.unit_price or 0,
                current_stock=0,
                is_active=False,  # inactive until admin verifies
            )
            db.add(new_item)
            await db.flush()
            inventory_item_id = new_item.id
            is_pending_verify = 1

    item = QuotationPartItem(
        quotation_id=quotation.id,
        part_name=payload.part_name,
        part_source=PartSource(payload.part_source),
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        purchase_price=payload.purchase_price or 0,
        total_price=round(payload.quantity * payload.unit_price),
        vendor_name=payload.vendor_name,
        bill_number=payload.bill_number,
        notes=notes_str or None,
        inventory_item_id=inventory_item_id,
        is_pending_verify=is_pending_verify,
    )
    db.add(item)
    await db.flush()
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    msg = "Part added successfully"
    if is_pending_verify == 1:
        msg += " (new part submitted for admin verification)"
    return success_response(data={"id": str(item.id), "total_price": item.total_price, "is_pending_verify": is_pending_verify}, message=msg)


@router.put("/{quotation_id}/parts/{part_id}", summary="Update part")
async def update_part_in_quotation(
    quotation_id: UUID,
    part_id: UUID,
    payload: UpdateQuotationPartRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")
    part = (
        await db.execute(select(QuotationPartItem).where(QuotationPartItem.id == part_id, QuotationPartItem.quotation_id == quotation.id, QuotationPartItem.is_active == True))
    ).scalar_one_or_none()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        if field == "part_source":
            setattr(part, field, PartSource(value))
        else:
            setattr(part, field, value)
    if payload.quantity is not None or payload.unit_price is not None:
        part.total_price = round(part.quantity * part.unit_price)
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(message="Part updated successfully")


@router.delete("/{quotation_id}/parts/{part_id}", summary="Delete part")
async def delete_part_from_quotation(
    quotation_id: UUID,
    part_id: UUID,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")
    part = (
        await db.execute(select(QuotationPartItem).where(QuotationPartItem.id == part_id, QuotationPartItem.quotation_id == quotation.id, QuotationPartItem.is_active == True))
    ).scalar_one_or_none()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    part.is_active = False
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(message="Part deleted successfully")




@router.post("/{quotation_id}/apply-coupon", summary="Apply or remove coupon from quotation")
async def apply_coupon_to_quotation(
    quotation_id: UUID,
    payload: dict = __import__('fastapi').Body(...),
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    from app.models.coupon import Coupon
    from datetime import timezone as _tz
    quotation = await _get_quotation_or_404(db, quotation_id)
    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    coupon_code_raw = payload.get("coupon_code", "").strip().upper() if payload.get("coupon_code") else ""

    if not coupon_code_raw:
        # Remove coupon from this quotation
        quotation.coupon_id = None
        quotation.coupon_code = None
        quotation.coupon_discount = 0.0
        await _recalculate_quotation(db, quotation)
        await db.commit()
        _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
        return success_response(data=_quotation_summary(quotation), message="Coupon removed")

    # Only allowed on the first quotation for this booking
    existing_count = (await db.execute(
        select(func.count()).select_from(
            select(Quotation).where(
                Quotation.booking_id == booking.id,
                Quotation.is_active == True,
                Quotation.id != quotation_id,  # exclude self
            ).subquery()
        )
    )).scalar_one()
    if existing_count > 0:
        raise HTTPException(status_code=400, detail="Coupon discount can only be applied to the first quotation for a booking")

    coupon = (await db.execute(
        select(Coupon).where(Coupon.code == coupon_code_raw, Coupon.is_active == True)
    )).scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail=f"Coupon '{coupon_code_raw}' not found or inactive")

    now_c = __import__('datetime').datetime.now(_tz.utc)
    if coupon.valid_until and coupon.valid_until.replace(tzinfo=_tz.utc) < now_c:
        raise HTTPException(status_code=400, detail="Coupon has expired")

    # ── Usage-limit check (idempotent / booking-aware) ────────────────────────
    # The booking already consumed ONE slot for this coupon when the customer placed the booking.
    # apply-coupon is only attaching the reference to the quotation (not a new usage).
    # So we:
    #   1. Never increment used_count here.
    #   2. For the usage-limit guard: if this is the booking's own coupon, subtract the
    #      booking's slot so the admin isn't blocked by its own prior count.
    booking_coupon_code = (getattr(booking, 'coupon_code', None) or '').strip().upper()
    is_booking_coupon = (coupon_code_raw == booking_coupon_code)
    if coupon.usage_limit:
        # Count how many OTHER bookings (not this one) have used this coupon
        from app.models.booking import Booking as _Booking
        other_uses_row = await db.execute(
            select(func.count()).select_from(
                select(_Booking).where(
                    _Booking.coupon_code == coupon.code,
                    _Booking.is_active == True,
                    _Booking.id != booking.id,
                ).subquery()
            )
        )
        other_uses = other_uses_row.scalar_one() or 0
        if other_uses >= coupon.usage_limit:
            raise HTTPException(status_code=400, detail="Coupon usage limit reached")
        # Sync used_count to reality (fix any inflated values from old code)
        correct_total = (await db.execute(
            select(func.count()).select_from(
                select(_Booking).where(
                    _Booking.coupon_code == coupon.code,
                    _Booking.is_active == True,
                ).subquery()
            )
        )).scalar_one() or 0
        if coupon.used_count != correct_total:
            coupon.used_count = correct_total
    # ─────────────────────────────────────────────────────────────────────────

    # Attach coupon to quotation — _recalculate_quotation computes the actual discount
    quotation.coupon_id = coupon.id
    quotation.coupon_code = coupon.code
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    disc = quotation.coupon_discount or 0.0
    return success_response(data=_quotation_summary(quotation), message=f"Coupon '{coupon_code_raw}' applied — discount ₹{disc}")

@router.post("/{quotation_id}/discount", summary="Apply discount")
async def apply_discount(
    quotation_id: UUID,
    payload: ApplyDiscountRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    quotation.discount_amount = payload.amount
    await _recalculate_quotation(db, quotation)
    await _add_status_log(db, quotation, current_user["user_id"], payload.notes or "Discount applied")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Discount applied successfully")


@router.post("/{quotation_id}/adjustment", summary="Apply adjustment")
async def apply_adjustment(
    quotation_id: UUID,
    payload: ApplyAdjustmentRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    quotation.adjustment_amount = payload.amount
    await _recalculate_quotation(db, quotation)
    await _add_status_log(db, quotation, current_user["user_id"], payload.notes or "Adjustment applied")
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data=_quotation_summary(quotation), message="Adjustment applied successfully")


@router.delete("/{quotation_id}/services/{item_id}", summary="Remove service from quotation")
async def delete_service_from_quotation(
    quotation_id: UUID,
    item_id: UUID,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")
    item = (
        await db.execute(
            select(QuotationServiceItem).where(
                QuotationServiceItem.id == item_id,
                QuotationServiceItem.quotation_id == quotation.id,
                QuotationServiceItem.is_active == True,
            )
        )
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Service item not found")
    item.is_active = False
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(message="Service removed from quotation")


@router.put("/{quotation_id}/services/{item_id}", summary="Update service item in quotation")
async def update_service_in_quotation(
    quotation_id: UUID,
    item_id: UUID,
    payload: AddQuotationServiceRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")
    item = (
        await db.execute(
            select(QuotationServiceItem).where(
                QuotationServiceItem.id == item_id,
                QuotationServiceItem.quotation_id == quotation.id,
                QuotationServiceItem.is_active == True,
            )
        )
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Service item not found")
    if payload.unit_price is not None:
        item.unit_price = payload.unit_price
    if payload.quantity is not None:
        item.quantity = payload.quantity
    item.total_price = round(item.unit_price * item.quantity)
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(data={"id": str(item.id), "total_price": item.total_price}, message="Service updated")


# ══════════════════════════════════════════════════════════════════════════════
# QUOTATION APPLIANCES — link CustomerAppliance records to a quotation
# ══════════════════════════════════════════════════════════════════════════════


class AddQuotationApplianceRequest(BaseModel):
    appliance_id: Optional[str] = None   # CustomerAppliance.id (nullable = manual entry)
    appliance_label: str                  # display label for service_name prefix

class MarkRepeatComplaintRequest(BaseModel):
    appliance_label: str
    is_repeat: bool
    repeat_booking_id: Optional[str] = None

class RemoveQuotationApplianceRequest(BaseModel):
    appliance_label: str


@router.get("/{quotation_id}/appliances", summary="List appliances linked to quotation")
async def list_quotation_appliances(
    quotation_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """Return all appliances attached to this quotation with repeat-complaint info."""
    quotation = await _get_quotation_or_404(db, quotation_id)

    rows = (await db.execute(
        sa_text("""
            SELECT qa.id, qa.appliance_id, qa.appliance_label,
                   qa.is_repeat_complaint, qa.repeat_booking_id, qa.repeat_confirmed_at,
                   ca.brand_id, ca.model, ca.category, ca.serial_number,
                   ab.name AS brand_name,
                   rb.booking_number AS prev_booking_number,
                   rb.scheduled_date AS prev_date,
                   rb.service_name   AS prev_service,
                   t.name            AS prev_technician,
                   ash.work_done     AS prev_work_done,
                   ash.issue_reported AS prev_issue
            FROM quotation_appliances qa
            LEFT JOIN customer_appliances ca ON ca.id = qa.appliance_id
            LEFT JOIN appliance_brands ab ON ab.id = ca.brand_id
            LEFT JOIN bookings rb ON rb.id = qa.repeat_booking_id
            LEFT JOIN technicians t ON t.id = rb.technician_id
            LEFT JOIN appliance_service_history ash ON ash.booking_id = rb.id AND ash.appliance_id = qa.appliance_id
            WHERE qa.quotation_id = :qid AND qa.is_active = true
            ORDER BY qa.created_at
        """),
        {"qid": str(quotation_id)}
    )).mappings().all()

    result = []
    for r in rows:
        item = dict(r)
        item["id"] = str(item["id"])
        item["appliance_id"] = str(item["appliance_id"]) if item["appliance_id"] else None
        item["repeat_booking_id"] = str(item["repeat_booking_id"]) if item["repeat_booking_id"] else None
        item["repeat_confirmed_at"] = item["repeat_confirmed_at"].isoformat() if item["repeat_confirmed_at"] else None
        item["prev_date"] = item["prev_date"].isoformat() if item.get("prev_date") else None
        result.append(item)

    return success_response(data=result)


@router.post("/{quotation_id}/appliances", summary="Add appliance to quotation")
async def add_quotation_appliance(
    quotation_id: UUID,
    payload: AddQuotationApplianceRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    """Link a CustomerAppliance (or manual label) to this quotation."""
    from app.models.booking import Booking
    from app.models.customer import Customer
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(400, "Quotation is not editable")

    label = payload.appliance_label.strip()
    if not label:
        raise HTTPException(400, "appliance_label is required")

    # Check not already added
    existing = (await db.execute(
        sa_text("SELECT id FROM quotation_appliances WHERE quotation_id=:qid AND appliance_label=:lbl AND is_active=true"),
        {"qid": str(quotation_id), "lbl": label}
    )).first()
    if existing:
        raise HTTPException(400, f"Appliance '{label}' already added to this quotation")

    # Repeat complaint check — look for COMPLETED bookings for same customer + appliance within 30 days
    repeat_booking_id = None
    repeat_booking_info = None
    if payload.appliance_id:
        booking = (await db.execute(
            sa_text("SELECT b.id, b.booking_number, b.scheduled_date, b.service_name, b.customer_id FROM bookings b WHERE b.id = :bid"),
            {"bid": str(quotation.booking_id)}
        )).mappings().first()

        if booking:
            cutoff = now_ist() - timedelta(days=30)
            prev = (await db.execute(
                sa_text("""
                    SELECT b.id, b.booking_number, b.scheduled_date, b.service_name,
                           b.technician_id, t.name as technician_name,
                           ash.work_done, ash.issue_reported
                    FROM bookings b
                    JOIN appliance_service_history ash ON ash.booking_id = b.id
                    LEFT JOIN technicians t ON t.id = b.technician_id
                    WHERE ash.appliance_id = :aid
                      AND b.status = 'COMPLETED'
                      AND b.scheduled_date >= :cutoff
                      AND b.id != :current_bid
                    ORDER BY b.scheduled_date DESC
                    LIMIT 1
                """),
                {"aid": payload.appliance_id, "cutoff": cutoff, "current_bid": str(quotation.booking_id)}
            )).mappings().first()

            if prev:
                repeat_booking_id = prev["id"]
                repeat_booking_info = {
                    "booking_id": str(prev["id"]),
                    "booking_number": prev["booking_number"],
                    "scheduled_date": prev["scheduled_date"].isoformat() if prev["scheduled_date"] else None,
                    "service_name": prev["service_name"],
                    "technician_name": prev["technician_name"],
                    "work_done": prev["work_done"],
                    "issue_reported": prev["issue_reported"],
                }

    # Insert
    await db.execute(
        sa_text("""
            INSERT INTO quotation_appliances
              (id, quotation_id, appliance_id, appliance_label, is_repeat_complaint, repeat_booking_id, created_at, is_active)
            VALUES
              (gen_random_uuid(), :qid, :aid, :lbl, false, :rbid, NOW(), true)
        """),
        {
            "qid": str(quotation_id),
            "aid": payload.appliance_id or None,
            "lbl": label,
            "rbid": str(repeat_booking_id) if repeat_booking_id else None,
        }
    )

    # ── AUTO-ADD BOOKING SERVICE ────────────────────────────────────────────
    # When the FIRST appliance is added to a quotation that has no service items
    # yet, automatically add the booking's booked service as a service item
    # under this appliance. This saves the technician/CCO/admin from having to
    # manually re-add a service they already know from the booking.
    #
    # FIX: We flush before counting so the new appliance row is visible,
    # then commit the appliance row FIRST, and only then auto-insert the
    # service item in a separate flush+recalculate+commit cycle. This avoids
    # ORM cache issues where _recalculate_quotation can't see the raw-SQL
    # service insert in the same transaction unit.
    await db.commit()  # commit the appliance row first
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])

    # ── Now auto-add the booking service item (separate transaction) ──────
    try:
        existing_service_count = (await db.execute(
            sa_text("SELECT COUNT(*) FROM quotation_service_items WHERE quotation_id=:qid AND is_active=true"),
            {"qid": str(quotation_id)}
        )).scalar() or 0
        existing_appliance_count = (await db.execute(
            sa_text("SELECT COUNT(*) FROM quotation_appliances WHERE quotation_id=:qid AND is_active=true"),
            {"qid": str(quotation_id)}
        )).scalar() or 0
        # Only auto-add if this is the FIRST appliance AND there are no service items yet
        if existing_appliance_count == 1 and existing_service_count == 0:
            booking_row = (await db.execute(
                sa_text("""
                    SELECT b.service_id, b.service_name, b.city_id
                    FROM bookings b WHERE b.id = :bid
                """),
                {"bid": str(quotation.booking_id)}
            )).mappings().first()
            if booking_row and booking_row["service_id"]:
                svc_row = (await db.execute(
                    sa_text("SELECT id, name, base_price FROM services WHERE id=:sid AND is_active=true"),
                    {"sid": str(booking_row["service_id"])}
                )).mappings().first()
                if svc_row:
                    # Resolve city price if available
                    unit_price = float(svc_row["base_price"] or 0)
                    if booking_row["city_id"]:
                        cp = (await db.execute(
                            sa_text("""
                                SELECT price FROM service_city_prices
                                WHERE service_id=:sid AND city_id=:cid AND is_active=true LIMIT 1
                            """),
                            {"sid": str(svc_row["id"]), "cid": str(booking_row["city_id"])}
                        )).scalar()
                        if cp is not None:
                            unit_price = float(cp)
                    encoded_name = f"{label} :: {svc_row['name']}"
                    await db.execute(
                        sa_text("""
                            INSERT INTO quotation_service_items
                              (id, quotation_id, service_id, service_name, quantity, unit_price, total_price,
                               appliance_label, is_repeat_complaint, is_pending_verify, created_at, is_active)
                            VALUES
                              (gen_random_uuid(), :qid, :sid, :sname, 1, :price, :price,
                               :lbl, false, 0, NOW(), true)
                        """),
                        {
                            "qid": str(quotation_id),
                            "sid": str(svc_row["id"]),
                            "sname": encoded_name,
                            "price": unit_price,
                            "lbl": label,
                        }
                    )
                    # Flush the raw-SQL insert so ORM queries in _recalculate see it
                    await db.flush()
                    # Expire ORM cache so QuotationServiceItem query re-reads from DB
                    db.expire_all()
                    # Re-fetch quotation to get fresh ORM state after expire_all
                    quotation = await _get_quotation_or_404(db, quotation_id)
                    await _recalculate_quotation(db, quotation)
                    await db.commit()
                    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    except Exception as _auto_svc_err:
        import logging as _log2
        _log2.getLogger(__name__).warning("Auto-add booking service to quotation failed: %s", _auto_svc_err)
        # Non-fatal — appliance was already committed above

    return success_response(data={
        "appliance_label": label,
        "appliance_id": payload.appliance_id,
        "repeat_detected": repeat_booking_info is not None,
        "repeat_booking": repeat_booking_info,
    }, message="Appliance added to quotation")


@router.post("/{quotation_id}/appliances/repeat", summary="Mark/unmark appliance group as repeat complaint")
async def mark_repeat_complaint(
    quotation_id: UUID,
    payload: MarkRepeatComplaintRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    """Mark an appliance group as repeat complaint — excludes its services/parts from invoice total."""
    quotation = await _get_quotation_or_404(db, quotation_id)

    label = payload.appliance_label.strip()
    confirmed_at = now_utc().replace(tzinfo=None) if payload.is_repeat else None  # naive UTC for DateTime column

    # Update quotation_appliances row
    await db.execute(
        sa_text("""
            UPDATE quotation_appliances
            SET is_repeat_complaint = :is_repeat,
                repeat_booking_id   = COALESCE(:rbid::uuid, repeat_booking_id),
                repeat_confirmed_at = :cat
            WHERE quotation_id = :qid AND appliance_label = :lbl AND is_active = true
        """),
        {
            "is_repeat": payload.is_repeat,
            "rbid": payload.repeat_booking_id,
            "cat": confirmed_at,
            "qid": str(quotation_id),
            "lbl": label,
        }
    )

    # Mark all service items for this appliance label
    prefix = label + " :: "
    await db.execute(
        sa_text("""
            UPDATE quotation_service_items
            SET is_repeat_complaint = :is_repeat
            WHERE quotation_id = :qid
              AND is_active = true
              AND (service_name LIKE :prefix OR appliance_label = :lbl)
        """),
        {"is_repeat": payload.is_repeat, "qid": str(quotation_id), "prefix": prefix + "%", "lbl": label}
    )

    # Mark all part items for this appliance label
    await db.execute(
        sa_text("""
            UPDATE quotation_part_items
            SET is_repeat_complaint = :is_repeat
            WHERE quotation_id = :qid
              AND is_active = true
              AND (notes LIKE :prefix OR notes LIKE :lbl_prefix)
        """),
        {"is_repeat": payload.is_repeat, "qid": str(quotation_id),
         "prefix": "appliance:" + label + "%", "lbl_prefix": "appliance:" + label + "|%"}
    )

    # Recalculate totals (repeat items count as 0 for invoice)
    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])

    return success_response(data={"appliance_label": label, "is_repeat_complaint": payload.is_repeat},
                            message=f"Appliance {'marked' if payload.is_repeat else 'unmarked'} as repeat complaint")


@router.delete("/{quotation_id}/appliances/{appliance_label_encoded}", summary="Remove appliance from quotation")
async def remove_quotation_appliance(
    quotation_id: UUID,
    appliance_label_encoded: str,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    """Remove an appliance group (and all its services/parts) from the quotation."""
    from urllib.parse import unquote
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(400, "Quotation is not editable")

    label = unquote(appliance_label_encoded).strip()

    # Soft-delete the appliance row
    await db.execute(
        sa_text("UPDATE quotation_appliances SET is_active=false WHERE quotation_id=:qid AND appliance_label=:lbl"),
        {"qid": str(quotation_id), "lbl": label}
    )

    # Soft-delete all service items for this label
    prefix = label + " :: "
    await db.execute(
        sa_text("""
            UPDATE quotation_service_items SET is_active=false
            WHERE quotation_id=:qid AND (service_name LIKE :prefix OR appliance_label=:lbl)
        """),
        {"qid": str(quotation_id), "prefix": prefix + "%", "lbl": label}
    )

    # Soft-delete all part items for this label
    await db.execute(
        sa_text("""
            UPDATE quotation_part_items SET is_active=false
            WHERE quotation_id=:qid AND (notes LIKE :prefix OR notes LIKE :lbl_prefix)
        """),
        {"qid": str(quotation_id),
         "prefix": "appliance:" + label + "%",
         "lbl_prefix": "appliance:" + label + "|%"}
    )

    await _recalculate_quotation(db, quotation)
    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])
    return success_response(message=f"Appliance '{label}' and all its items removed from quotation")


@router.get("/{quotation_id}/repeat-check/{customer_id}", summary="Check repeat complaints for customer appliances")
async def check_repeat_complaints(
    quotation_id: UUID,
    customer_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    For each CustomerAppliance of this customer, check if there's a COMPLETED booking
    for the same appliance within the last 30 days (repeat complaint detection).
    Returns list of appliances with repeat_detected flag and previous booking info.
    """
    cutoff = now_ist() - timedelta(days=30)
    booking = (await db.execute(
        sa_text("SELECT id FROM bookings WHERE id=:bid"),
        {"bid": str(quotation_id)}  # quotation_id used for context, not needed here
    )).first()

    rows = (await db.execute(
        sa_text("""
            SELECT
                ca.id, ca.brand_id, ca.model, ca.category, ca.serial_number,
                ca.appliance_category_id, ca.notes,
                ab.name AS brand_name,
                -- most recent completed booking for this appliance in last 30 days
                prev_b.id AS prev_booking_id,
                prev_b.booking_number AS prev_booking_number,
                prev_b.scheduled_date AS prev_date,
                prev_b.service_name AS prev_service,
                ash.work_done AS prev_work_done,
                ash.issue_reported AS prev_issue,
                t.name AS prev_technician
            FROM customer_appliances ca
            LEFT JOIN appliance_brands ab ON ab.id = ca.brand_id
            LEFT JOIN LATERAL (
                SELECT b.id, b.booking_number, b.scheduled_date, b.service_name, b.technician_id
                FROM appliance_service_history ash2
                JOIN bookings b ON b.id = ash2.booking_id
                WHERE ash2.appliance_id = ca.id
                  AND b.status = 'COMPLETED'
                  AND b.scheduled_date >= :cutoff
                ORDER BY b.scheduled_date DESC
                LIMIT 1
            ) prev_b ON true
            LEFT JOIN appliance_service_history ash ON ash.booking_id = prev_b.id AND ash.appliance_id = ca.id
            LEFT JOIN technicians t ON t.id = prev_b.technician_id
            WHERE ca.customer_id = :cid AND ca.is_active = true
            ORDER BY ca.created_at DESC
        """),
        {"cid": str(customer_id), "cutoff": cutoff}
    )).mappings().all()

    result = []
    for r in rows:
        item = {
            "id": str(r["id"]),
            "brand_name": r["brand_name"],
            "model": r["model"],
            "category": r["category"],
            "serial_number": r["serial_number"],
            "notes": r["notes"],
            "label": " ".join(filter(None, [r["brand_name"], r["model"] or r["category"]])) or r["category"] or "Appliance",
            "repeat_detected": r["prev_booking_id"] is not None,
            "prev_booking": {
                "booking_id": str(r["prev_booking_id"]),
                "booking_number": r["prev_booking_number"],
                "scheduled_date": r["prev_date"].isoformat() if r["prev_date"] else None,
                "service_name": r["prev_service"],
                "work_done": r["prev_work_done"],
                "issue_reported": r["prev_issue"],
                "technician_name": r["prev_technician"],
            } if r["prev_booking_id"] else None,
        }
        result.append(item)

    return success_response(data=result)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN REPAIR ENDPOINT — fixes inflated coupon used_count and missing coupon
# refs on quotations caused by the old double-increment bug.
# Safe to call multiple times (idempotent).
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/repair-coupon-counts", summary="[Admin] Fix inflated coupon used_count values")
async def repair_coupon_counts(
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    """
    Repairs two things caused by the old double-increment bug:
    1. Syncs coupon.used_count to the actual number of bookings that used each coupon.
    2. For quotations where booking has a coupon but quotation.coupon_id is NULL,
       attaches the coupon reference so _recalculate_quotation can apply the discount.
    """
    from app.models.coupon import Coupon as _Coupon
    from app.models.booking import Booking as _Booking

    # Step 1: Fix used_count for all coupons
    coupons = (await db.execute(select(_Coupon).where(_Coupon.is_active == True))).scalars().all()
    fixed_coupons = []
    for cpn in coupons:
        real_count = (await db.execute(
            select(func.count()).select_from(
                select(_Booking).where(
                    _Booking.coupon_code == cpn.code,
                    _Booking.is_active == True,
                ).subquery()
            )
        )).scalar_one() or 0
        if (cpn.used_count or 0) != real_count:
            fixed_coupons.append({"code": cpn.code, "old": cpn.used_count, "new": real_count})
            cpn.used_count = real_count

    # Step 2: Fix quotations missing coupon_id where booking has a coupon
    # Find: quotations where coupon_id IS NULL but booking.coupon_code IS NOT NULL
    # and this is the first (only) quotation for that booking
    broken_quotations = (await db.execute(
        select(Quotation).where(
            Quotation.is_active == True,
            Quotation.coupon_id == None,
            Quotation.coupon_code == None,
        )
    )).scalars().all()

    repaired_quotations = []
    for q in broken_quotations:
        bk = (await db.execute(select(_Booking).where(_Booking.id == q.booking_id))).scalar_one_or_none()
        if not bk:
            continue
        bk_coupon_code = (getattr(bk, 'coupon_code', None) or '').strip().upper()
        if not bk_coupon_code:
            continue
        # Is this the only/first quotation for this booking?
        q_count = (await db.execute(
            select(func.count()).select_from(
                select(Quotation).where(
                    Quotation.booking_id == bk.id,
                    Quotation.is_active == True,
                ).subquery()
            )
        )).scalar_one()
        if q_count != 1:
            continue  # only fix if it's the sole quotation
        # Find the coupon
        cpn = (await db.execute(
            select(_Coupon).where(_Coupon.code == bk_coupon_code)
        )).scalar_one_or_none()
        if not cpn:
            continue
        q.coupon_id = cpn.id
        q.coupon_code = cpn.code
        await _recalculate_quotation(db, q)
        repaired_quotations.append({
            "quotation_number": q.quotation_number,
            "coupon_code": bk_coupon_code,
            "new_coupon_discount": q.coupon_discount,
        })

    await db.commit()
    # NOTE: No broadcast here — this is a bulk-repair utility, not a single quotation mutation.
    return success_response(data={
        "coupons_fixed": fixed_coupons,
        "quotations_repaired": repaired_quotations,
    }, message=f"Repair complete: {len(fixed_coupons)} coupon counts fixed, {len(repaired_quotations)} quotations repaired")


# ─── Quotation PDF download ───────────────────────────────────────────────────
from io import BytesIO
from fastapi.responses import StreamingResponse
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors as rl_colors
from reportlab.platypus import (Table, TableStyle, Paragraph, Spacer,
                                 SimpleDocTemplate, HRFlowable, Image, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
import logging as _logging
_pdf_logger = _logging.getLogger(__name__)


def _qt_fetch_logo(url: str):
    """Best-effort logo fetch for quotation PDF. Returns bytes or None."""
    if not url:
        return None
    try:
        import requests as _req
        r = _req.get(url, timeout=5)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def _qt_num_to_words(n: float) -> str:
    """Convert a number to Indian-style English words."""
    try:
        from num2words import num2words
        return num2words(int(round(n)), lang="en_IN").title() + " Rupees Only"
    except Exception:
        pass
    try:
        n = int(round(n))
        if n == 0:
            return "Zero Rupees Only"
        ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
                "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
                "Seventeen", "Eighteen", "Nineteen"]
        tens_w = ["", "", "Twenty", "Thirty", "Forty", "Fifty",
                  "Sixty", "Seventy", "Eighty", "Ninety"]
        def _h(num):
            if num < 20: return ones[num]
            if num < 100: return tens_w[num // 10] + (" " + ones[num % 10] if num % 10 else "")
            return ones[num // 100] + " Hundred" + (" " + _h(num % 100) if num % 100 else "")
        parts_w = []
        if n >= 10000000: parts_w.append(_h(n // 10000000) + " Crore"); n %= 10000000
        if n >= 100000:   parts_w.append(_h(n // 100000)   + " Lakh");  n %= 100000
        if n >= 1000:     parts_w.append(_h(n // 1000)     + " Thousand"); n %= 1000
        if n > 0:         parts_w.append(_h(n))
        return " ".join(parts_w) + " Rupees Only"
    except Exception:
        return ""


def _build_quotation_pdf(quotation, booking, customer, domain_profile, services, parts, cust_address=None) -> bytes:
    """
    Professional Quotation PDF — identical visual language to the Invoice PDF.

    Layout (A4, 16 mm margins):
    ┌─────────────────────────────────────────────────────────────────────────┐
    │  [LOGO 36mm]  Business Name (navy, 17pt)          QUOTATION             │
    │               Tagline, Address                    QT-XXXXXXXXXXXX       │
    │               Phone | Email                       Date: DD Mon YYYY     │
    │               GSTIN | PAN                         [Pending Approval]    │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  QUOTATION TO                  │  BOOKING DETAILS                       │
    │  Customer Name (bold)          │  Booking No.  #BK-XXXXXX              │
    │  Phone                         │  Service      AC Gas Charging          │
    │  Address                       │  Scheduled    06 Jul 2026             │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  #  │ Description    │ Type    │ Qty │ Rate (INR) │ Amount (INR)        │
    │  1  │ Gas Charging   │ Service │  1  │    850.00  │       850.00        │
    │  2  │ R22 Refrigerant│ Part    │  1  │    650.00  │       650.00        │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                               │ Subtotal       INR  1,500.00            │
    │  Amount in Words (italic)     │ Discount       - INR   0.00            │
    │                               │ Tax (18%)      INR    270.00            │
    │                               │ ─────────────────────────────           │
    │                               │ TOTAL AMOUNT   INR  1,770.00            │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Notes / Remarks (if any)                                               │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Terms: Valid 7 days · Estimates may vary · No physical signature       │
    │  (c) 2026 BusinessName · Computer-generated quotation                   │
    └─────────────────────────────────────────────────────────────────────────┘
    """
    from io import BytesIO as _BIO

    # ── Palette (matches invoice exactly) ─────────────────────────────────────
    NAVY     = rl_colors.HexColor("#1E3A8A")
    BLUE     = rl_colors.HexColor("#2563EB")
    BLUE_LT  = rl_colors.HexColor("#DBEAFE")
    BLUE_XLT = rl_colors.HexColor("#EFF6FF")
    ORANGE   = rl_colors.HexColor("#EA580C")
    GREEN    = rl_colors.HexColor("#16A34A")
    GREY_DK  = rl_colors.HexColor("#111827")
    GREY_MD  = rl_colors.HexColor("#6B7280")
    GREY_LT  = rl_colors.HexColor("#F9FAFB")
    WHITE    = rl_colors.white
    DIVIDER  = rl_colors.HexColor("#E2E8F0")

    W = 178 * mm  # usable page width (A4 − 16 mm × 2)

    # ── Style factory ─────────────────────────────────────────────────────────
    base = getSampleStyleSheet()["Normal"]
    def S(name, size=9, color=GREY_DK, bold=False, italic=False,
          align=0, leading=None, sb=0, sa=0):
        fn = ("Helvetica-BoldOblique" if (bold and italic) else
              "Helvetica-Bold"         if bold              else
              "Helvetica-Oblique"      if italic            else "Helvetica")
        return ParagraphStyle(name, parent=base,
                              fontSize=size, textColor=color, fontName=fn,
                              alignment=align, leading=leading or round(size * 1.4),
                              spaceBefore=sb, spaceAfter=sa)

    # Header styles
    sH_biz   = S("QHBiz",  17, NAVY,    bold=True)
    sH_tag   = S("QHTag",   9, GREY_MD, italic=True)
    sH_addr  = S("QHAddr",  8, GREY_MD)
    sH_gst   = S("QHGST",   8, GREY_MD)
    # Badge
    sB_title = S("QBTit",  11, WHITE,   bold=True,  align=2)
    sB_num   = S("QBNum",   8, rl_colors.HexColor("#BFDBFE"), align=2)
    sB_date  = S("QBDat",   8, rl_colors.HexColor("#93C5FD"), align=2)
    # Section
    sS_lbl   = S("QSLbl",   7, GREY_MD, bold=True)
    sS_val   = S("QSVal",   9, GREY_DK, bold=True)
    sS_sub   = S("QSSub",   8, GREY_MD)
    # Table
    sTH      = S("QTH",     9, WHITE,   bold=True)
    sTD      = S("QTD",     9, GREY_DK)
    sTDr     = S("QTDr",    9, GREY_DK, align=2)
    sTDbr    = S("QTDbr",   9, GREY_DK, bold=True, align=2)
    # Totals
    sTLbl    = S("QTLbl",   9, GREY_MD)
    sTVal    = S("QTVal",   9, GREY_DK, bold=True, align=2)
    sTGLbl   = S("QTGLbl", 10, NAVY,   bold=True)
    sTGVal   = S("QTGVal", 10, ORANGE, bold=True, align=2)
    sTDLbl   = S("QTDLbl",  9, GREEN)
    sTDVal   = S("QTDVal",  9, GREEN,  align=2)
    sWords   = S("QWords",  7, GREY_MD, italic=True, align=2)
    sNote    = S("QNote",   8, GREY_MD)
    sPayH    = S("QPayH",   9, NAVY,   bold=True)
    sFooter  = S("QFoot",   8, GREY_MD, align=1)
    sFooter2 = S("QFoot2",  7, rl_colors.HexColor("#9CA3AF"), align=1)

    # ── Collect business info from domain_profile (dict passed by endpoint) ───
    # domain_profile is a plain dict with keys matching DomainProfile columns.
    dp = domain_profile or {}

    biz_name  = (dp.get("business_legal_name") or dp.get("business_name") or "Bibek Enterprises")
    tagline   = dp.get("tagline") or None
    logo_url  = dp.get("logo_url") or None
    gstin     = dp.get("gstin") or None
    pan       = dp.get("pan_number") or None
    phone     = dp.get("support_phone") or None
    email_str = dp.get("support_email") or None
    # Address from profile
    addr_parts = []
    if dp.get("office_address"): addr_parts.append(dp["office_address"])
    city_line  = ", ".join(filter(None, [dp.get("office_city"), dp.get("office_state")]))
    if dp.get("office_pincode"): city_line += f" - {dp['office_pincode']}"
    if city_line: addr_parts.append(city_line)
    copyright_txt = (dp.get("copyright_text") or
                     f"(c) {now_ist().year} {biz_name}. All rights reserved.")

    # ── Customer info ─────────────────────────────────────────────────────────
    cust_name   = (getattr(customer, "name", None) or
                   f"{getattr(customer, 'first_name', '') or ''} {getattr(customer, 'last_name', '') or ''}".strip() or
                   "Customer")
    cust_mobile = (getattr(customer, "mobile", None) or
                   getattr(customer, "mobile_number", None) or "")

    # ── Booking info ──────────────────────────────────────────────────────────
    booking_no  = (booking.booking_number if booking else "—")
    service_nm  = (booking.service_name   if (booking and getattr(booking, "service_name", None)) else "Service")
    sched_str   = ""
    try:
        if booking and booking.scheduled_date:
            sd = booking.scheduled_date
            sched_str = sd.strftime("%d %b %Y") if hasattr(sd, "strftime") else str(sd)[:10]
    except Exception:
        pass
    addr_str_bk = ""
    try:
        if booking:
            for field in ("address_line", "address", "address_str"):
                v = getattr(booking, field, None)
                if v:
                    addr_str_bk = str(v)[:100]
                    break
    except Exception:
        pass

    # ── Quotation meta ────────────────────────────────────────────────────────
    created_str = ""
    try:
        cd = quotation.created_at
        created_str = cd.strftime("%d %b %Y") if hasattr(cd, "strftime") else str(cd)[:10]
    except Exception:
        pass

    _status_raw = str(quotation.status.value if hasattr(quotation.status, "value") else quotation.status)
    status_label = {
        "DRAFT":                "Draft",
        "SUBMITTED":            "Pending Approval",
        "APPROVED":             "Approved",
        "REJECTED":             "Rejected",
        "REVISED":              "Revised",
        "CONVERTED_TO_INVOICE": "Converted to Invoice",
        "EXPIRED":              "Expired",
    }.get(_status_raw, "Pending Approval")
    badge_bg = {
        "APPROVED":             rl_colors.HexColor("#059669"),
        "REJECTED":             rl_colors.HexColor("#DC2626"),
        "CONVERTED_TO_INVOICE": rl_colors.HexColor("#7C3AED"),
        "EXPIRED":              GREY_MD,
    }.get(_status_raw, ORANGE)

    # ── Document ──────────────────────────────────────────────────────────────
    buf = _BIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=14*mm, bottomMargin=14*mm,
                            leftMargin=16*mm, rightMargin=16*mm,
                            title=f"Quotation {quotation.quotation_number}")
    els = []

    # ═══ 1. HEADER ═══════════════════════════════════════════════════════════
    # Logo — wide landscape format (4:1 ratio, matching admin upload dimensions)
    LOGO_W = 60 * mm
    LOGO_H = 15 * mm

    logo_cell = None
    logo_bytes = _qt_fetch_logo(logo_url)
    if logo_bytes:
        try:
            logo_cell = Image(_BIO(logo_bytes), width=LOGO_W, height=LOGO_H)
        except Exception:
            logo_cell = None

    if logo_cell is None:
        # Navy monogram fallback
        initials = "".join(w[0].upper() for w in biz_name.split()[:2])
        mono_p = Paragraph(f"<b>{initials}</b>", S("QMono", 16, WHITE, bold=True, align=1))
        mono_t = Table([[mono_p]], colWidths=[LOGO_W], rowHeights=[LOGO_H])
        mono_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), NAVY),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("ALIGN",         (0,0),(-1,-1), "CENTER"),
            ("ROUNDEDCORNERS",[4]),
        ]))
        logo_cell = mono_t

    # Business info column
    biz_col = [Paragraph(biz_name, sH_biz)]
    if tagline:
        biz_col.append(Paragraph(tagline, sH_tag))
    for a in addr_parts:
        biz_col.append(Paragraph(a, sH_addr))
    contact = "  |  ".join(filter(None, [phone, email_str]))
    if contact:
        biz_col.append(Paragraph(contact, sH_addr))
    gst_str = "  |  ".join(filter(None, [
        f"GSTIN: {gstin}" if gstin else None,
        f"PAN: {pan}"     if pan    else None,
    ]))
    if gst_str:
        biz_col.append(Paragraph(gst_str, sH_gst))

    # Badge column (navy box + status pill, matches invoice)
    badge_rows = [
        [Paragraph("<b>QUOTATION</b>", sB_title)],
        [Paragraph(quotation.quotation_number, sB_num)],
        [Paragraph(f"Date: {created_str}", sB_date)],
    ]
    badge_t = Table(badge_rows, colWidths=[46*mm])
    badge_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), NAVY),
        ("TOPPADDING",    (0,0),(-1,-1), 5),
        ("BOTTOMPADDING", (0,0),(-1,-1), 5),
        ("LEFTPADDING",   (0,0),(-1,-1), 8),
        ("RIGHTPADDING",  (0,0),(-1,-1), 8),
        ("ROUNDEDCORNERS",[4]),
    ]))
    pill_t = Table([[Paragraph(status_label, S("QPill", 8, WHITE, bold=True, align=1))]],
                   colWidths=[46*mm], rowHeights=[14])
    pill_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), badge_bg),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ("ROUNDEDCORNERS",[3]),
    ]))
    badge_col = [badge_t, Spacer(1, 2*mm), pill_t]

    # Logo col width = LOGO_W + 4mm padding; badge col = 50mm; rest = biz
    logo_col_w = LOGO_W + 4*mm
    badge_col_w = 50*mm
    biz_col_w   = W - logo_col_w - badge_col_w

    hdr_t = Table([[logo_cell, biz_col, badge_col]],
                  colWidths=[logo_col_w, biz_col_w, badge_col_w])
    hdr_t.setStyle(TableStyle([
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("RIGHTPADDING", (0,0),(0,0),   10),
        ("LEFTPADDING",  (2,0),(2,0),    4),
    ]))
    els.append(hdr_t)
    els.append(Spacer(1, 3*mm))

    # Full-width blue accent rule (matches invoice)
    rule_t = Table([[""]], colWidths=[W], rowHeights=[2])
    rule_t.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,-1), BLUE)]))
    els.append(rule_t)
    els.append(Spacer(1, 5*mm))

    # ═══ 2. QUOTATION TO / BOOKING DETAILS ══════════════════════════════════
    bill_content = [Paragraph("QUOTATION TO", sS_lbl), Paragraph(cust_name, sS_val)]
    if cust_mobile: bill_content.append(Paragraph(cust_mobile, sS_sub))
    # Full address: address_line1 (from CustomerAddress or booking.address_line), address_line2, city, state - pincode
    _qt_addr_parts = []
    _qt_line1 = (getattr(cust_address, "address_line1", None) if cust_address else None) or addr_str_bk
    if _qt_line1: _qt_addr_parts.append(_qt_line1)
    if cust_address and getattr(cust_address, "address_line2", None):
        _qt_addr_parts.append(cust_address.address_line2)
    _qt_city  = (getattr(cust_address, "city",  None) if cust_address else None) or (booking.city if booking else "") or ""
    _qt_state = (getattr(cust_address, "state", None) if cust_address else None) or ""
    _qt_pin   = (getattr(cust_address, "pincode", None) if cust_address else None) or (booking.pincode if booking else "") or ""
    _qt_city_state = ", ".join(filter(None, [_qt_city, _qt_state]))
    if _qt_pin: _qt_city_state += f" - {_qt_pin}"
    if _qt_city_state: _qt_addr_parts.append(_qt_city_state)
    for _qt_al in _qt_addr_parts:
        bill_content.append(Paragraph(_qt_al, sS_sub))

    bk_content = [Paragraph("BOOKING DETAILS", sS_lbl)]
    bk_content.append(Paragraph(booking_no, sS_val))
    if service_nm:  bk_content.append(Paragraph(f"Service: {service_nm}", sS_sub))
    if sched_str:   bk_content.append(Paragraph(f"Scheduled: {sched_str}", sS_sub))

    info_t = Table([[bill_content, bk_content]], colWidths=[W / 2, W / 2])
    info_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), BLUE_XLT),
        ("LEFTPADDING",   (0,0),(-1,-1), 10),
        ("RIGHTPADDING",  (0,0),(-1,-1), 10),
        ("TOPPADDING",    (0,0),(-1,-1), 8),
        ("BOTTOMPADDING", (0,0),(-1,-1), 8),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("LINEAFTER",     (0,0),(0,-1),  0.5, BLUE_LT),
        ("LINEBELOW",     (0,-1),(-1,-1), 1, BLUE),
        ("ROUNDEDCORNERS",[3]),
    ]))
    els.append(info_t)
    els.append(Spacer(1, 5*mm))

    # ═══ 3. LINE ITEMS TABLE ══════════════════════════════════════════════════
    # Column widths matching invoice: #, Description, Type, Qty, Rate, Amount
    CW = [10*mm, 76*mm, 20*mm, 14*mm, 28*mm, 28*mm]
    rows = [[
        Paragraph("#",           sTH),
        Paragraph("Description", sTH),
        Paragraph("Type",        sTH),
        Paragraph("Qty",         sTH),
        Paragraph("Rate (INR)",  sTH),
        Paragraph("Amt (INR)",   sTH),
    ]]
    idx = 1
    for s in services:
        name = (getattr(s, "service_name", None) or
                getattr(s, "custom_service_name", None) or "Service")
        rows.append([
            Paragraph(str(idx), sTDr),
            Paragraph(name, sTD),
            Paragraph("Service", sTD),
            Paragraph(str(s.quantity), sTDr),
            Paragraph(f"{float(s.unit_price):,.2f}", sTDr),
            Paragraph(f"{float(s.total_price):,.2f}", sTDbr),
        ])
        idx += 1
    for p in parts:
        name = getattr(p, "part_name", None) or "Part"
        rows.append([
            Paragraph(str(idx), sTDr),
            Paragraph(name, sTD),
            Paragraph("Part", sTD),
            Paragraph(str(p.quantity), sTDr),
            Paragraph(f"{float(p.unit_price):,.2f}", sTDr),
            Paragraph(f"{float(p.total_price):,.2f}", sTDbr),
        ])
        idx += 1
    if not services and not parts:
        # Fallback row if no items
        total_fb = float(quotation.total_amount or 0)
        rows.append([
            Paragraph("1", sTDr),
            Paragraph("Service Charges", sTD),
            Paragraph("Service", sTD),
            Paragraph("1", sTDr),
            Paragraph(f"{total_fb:,.2f}", sTDr),
            Paragraph(f"{total_fb:,.2f}", sTDbr),
        ])

    nr = len(rows)
    items_t = Table(rows, colWidths=CW, repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,0),  NAVY),
        ("LINEBELOW",     (0,0),(-1,0),  1.5, BLUE),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
        ("LINEBELOW",     (0,1),(-1,-2), 0.3, DIVIDER),
        ("LINEBELOW",     (0,nr-1),(-1,nr-1), 1.5, NAVY),
        ("TOPPADDING",    (0,0),(-1,-1), 7),
        ("BOTTOMPADDING", (0,0),(-1,-1), 7),
        ("LEFTPADDING",   (0,0),(-1,-1), 6),
        ("RIGHTPADDING",  (0,0),(-1,-1), 6),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
    ]))
    els.append(items_t)
    els.append(Spacer(1, 5*mm))

    # ═══ 4. TOTALS ════════════════════════════════════════════════════════════
    subtotal = float(getattr(quotation, "subtotal_amount", 0) or 0)
    disc     = float(quotation.discount_amount or 0)
    adj      = float(getattr(quotation, "adjustment_amount", 0) or 0)
    tax_pct  = float(quotation.tax_percent or 0)
    tax_amt  = float(quotation.tax_amount or 0)
    total    = float(quotation.total_amount or 0)
    # Derive subtotal if not set
    if subtotal <= 0:
        subtotal = total - tax_amt + disc - adj

    # Determine GST mode from quotation
    _qt_tax_mode = str(getattr(quotation, "tax_mode", "B2C") or "B2C").upper()
    _qt_is_non_gst = (_qt_tax_mode == "NONE")

    tot_rows = []
    tot_rows.append([Paragraph("Subtotal", sTLbl),
                     Paragraph(f"INR {subtotal:,.2f}", sTVal)])
    if disc > 0:
        tot_rows.append([Paragraph("Discount", sTDLbl),
                         Paragraph(f"- INR {disc:,.2f}",
                                   S("QDisc", 9, rl_colors.HexColor("#DC2626"), align=2))])
    if adj != 0:
        tot_rows.append([Paragraph("Adjustment", sTLbl),
                         Paragraph(f"INR {adj:,.2f}", sTVal)])
    # Show tax only for GST quotations
    if not _qt_is_non_gst and tax_amt > 0:
        # Split into CGST+SGST for B2C/B2B style
        if _qt_tax_mode in ("B2C", "B2B") and tax_pct > 0:
            half = tax_amt / 2
            half_pct = tax_pct / 2
            tot_rows.append([Paragraph(f"CGST ({half_pct:.1f}%)", sTLbl),
                             Paragraph(f"INR {half:,.2f}", sTVal)])
            tot_rows.append([Paragraph(f"SGST ({half_pct:.1f}%)", sTLbl),
                             Paragraph(f"INR {half:,.2f}", sTVal)])
        else:
            lbl = f"Tax ({tax_pct:.0f}%)" if tax_pct > 0 else "Tax"
            tot_rows.append([Paragraph(lbl, sTLbl),
                             Paragraph(f"INR {tax_amt:,.2f}", sTVal)])

    # Grand total row (highlighted)
    tot_rows.append([Paragraph("TOTAL AMOUNT", sTGLbl),
                     Paragraph(f"INR {total:,.2f}", sTGVal)])

    grand_idx = len(tot_rows) - 1
    tot_t = Table(tot_rows, colWidths=[42*mm, 38*mm], hAlign="RIGHT")
    tot_t.setStyle(TableStyle([
        ("LINEBELOW",     (0,0),(-1, grand_idx-1), 0.4, DIVIDER),
        ("LINEABOVE",     (0, grand_idx),(-1, grand_idx), 1.5, NAVY),
        ("LINEBELOW",     (0, grand_idx),(-1, grand_idx), 1.5, NAVY),
        ("BACKGROUND",    (0, grand_idx),(-1, grand_idx), BLUE_XLT),
        ("TOPPADDING",    (0,0),(-1,-1), 4),
        ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ("LEFTPADDING",   (0,0),(-1,-1), 6),
        ("RIGHTPADDING",  (0,0),(-1,-1), 6),
        ("ROUNDEDCORNERS",[3]),
    ]))

    words_str = _qt_num_to_words(total)
    els.append(Table([[Paragraph("", sNote), tot_t]],
                     colWidths=[W - 82*mm, 82*mm],
                     style=TableStyle([("VALIGN", (0,0),(-1,-1), "TOP")])))
    if words_str:
        els.append(Table([[Paragraph("", sNote),
                           Paragraph(words_str, sWords)]],
                         colWidths=[W - 82*mm, 82*mm]))
    els.append(Spacer(1, 7*mm))

    # ═══ 5. REMARKS ══════════════════════════════════════════════════════════
    if getattr(quotation, "remarks", None):
        remarks_block = [
            Paragraph("Notes / Remarks", sPayH),
            Paragraph(quotation.remarks, sNote),
        ]
        els.append(KeepTogether(remarks_block))
        els.append(Spacer(1, 5*mm))

    # ═══ 6. TERMS & FOOTER ════════════════════════════════════════════════════
    terms_block = [Paragraph("Terms & Conditions", sPayH)]
    for t in [
        "1. This quotation is computer-generated and valid for 7 days from the date of issue.",
        "2. Prices are estimates and may vary based on actual parts and labour during the visit.",
        "3. This document does not require a physical signature.",
    ]:
        terms_block.append(Paragraph(t, sNote))
    els.append(KeepTogether(terms_block))
    els.append(Spacer(1, 7*mm))

    foot_rule = Table([[""]], colWidths=[W], rowHeights=[0.8])
    foot_rule.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,-1), BLUE_LT)]))
    els.append(foot_rule)
    els.append(Spacer(1, 3*mm))
    els.append(Paragraph(copyright_txt, sFooter))
    els.append(Paragraph(
        "This is a computer-generated quotation and does not require a physical signature.",
        sFooter2,
    ))

    doc.build(els)
    buf.seek(0)
    return buf.read()


@router.get("/{quotation_id}/pdf", summary="Download Quotation PDF")
async def get_quotation_pdf(
    quotation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(AnyAuthenticated),
):
    """
    Generate and stream a professional quotation PDF.
    Accessible by the customer who owns the booking, the assigned technician,
    admin, CCO, and accountant roles.
    """
    from sqlalchemy import select
    from app.models.customer import Customer
    from app.models.booking import Booking
    from app.models.domain import Domain

    quotation = await _get_quotation_or_404(db, quotation_id)
    await _ensure_access(db, quotation, current_user)

    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    customer = (await db.execute(select(Customer).where(Customer.id == booking.customer_id))).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Load domain profile for branding (DomainProfile has logo, address, GSTIN, etc.)
    domain_profile = None
    try:
        from app.models.domain import DomainProfile as _DomainProfile
        domain_id = getattr(booking, "domain_id", None)
        if domain_id:
            dp_obj = (await db.execute(
                select(_DomainProfile).where(_DomainProfile.domain_id == domain_id)
            )).scalar_one_or_none()
            if dp_obj:
                # Convert ORM object to plain dict so _build_quotation_pdf can use .get()
                domain_profile = {
                    "business_name":      getattr(dp_obj, "business_legal_name", None) or "",
                    "business_legal_name":getattr(dp_obj, "business_legal_name", None) or "",
                    "tagline":            getattr(dp_obj, "tagline", None) or "",
                    "logo_url":           getattr(dp_obj, "logo_url", None) or "",
                    "gstin":              getattr(dp_obj, "gstin", None) or "",
                    "pan_number":         getattr(dp_obj, "pan_number", None) or "",
                    "support_phone":      getattr(dp_obj, "support_phone", None) or "",
                    "support_email":      getattr(dp_obj, "support_email", None) or "",
                    "office_address":     getattr(dp_obj, "office_address", None) or "",
                    "office_city":        getattr(dp_obj, "office_city", None) or "",
                    "office_state":       getattr(dp_obj, "office_state", None) or "",
                    "office_pincode":     getattr(dp_obj, "office_pincode", None) or "",
                    "copyright_text":     getattr(dp_obj, "copyright_text", None) or "",
                }
            else:
                # Fallback: load from Domain table basic info
                domain = (await db.execute(select(Domain).where(Domain.id == domain_id))).scalar_one_or_none()
                if domain:
                    domain_profile = {
                        "business_name":  getattr(domain, "business_name", None) or getattr(domain, "name", "") or "",
                        "logo_url":       getattr(domain, "logo_url", None) or "",
                        "gstin":          getattr(domain, "gstin", None) or "",
                        "support_phone":  getattr(domain, "support_phone", None) or "",
                        "support_email":  getattr(domain, "support_email", None) or "",
                    }
    except Exception:
        domain_profile = {}

    # Load services and parts line items
    services = (await db.execute(
        select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == quotation.id)
    )).scalars().all()
    parts = (await db.execute(
        select(QuotationPartItem).where(QuotationPartItem.quotation_id == quotation.id)
    )).scalars().all()

    # Load CustomerAddress for full address display in PDF
    cust_address = None
    try:
        from app.models.customer import CustomerAddress as _CustAddr
        if booking and getattr(booking, "address_id", None):
            cust_address = (await db.execute(
                select(_CustAddr).where(_CustAddr.id == booking.address_id)
            )).scalar_one_or_none()
    except Exception:
        cust_address = None

    try:
        pdf_bytes = _build_quotation_pdf(quotation, booking, customer, domain_profile, services, parts, cust_address)
    except Exception:
        _pdf_logger.exception("Quotation PDF generation failed, falling back to plain layout")
        buf = BytesIO()
        c = rl_canvas.Canvas(buf)
        c.setTitle(quotation.quotation_number)
        c.drawString(50, 800, f"Quotation: {quotation.quotation_number}")
        c.drawString(50, 780, f"Total Amount: INR {float(quotation.total_amount or 0):.2f}")
        c.showPage()
        c.save()
        buf.seek(0)
        pdf_bytes = buf.read()

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={quotation.quotation_number}.pdf"},
    )
