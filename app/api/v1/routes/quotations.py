import asyncio
from app.core.background_tasks import track_task
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
from app.utils.response import success_response
from app.utils.notify import push_to_technician

router = APIRouter()

EDITABLE_STATUSES = {QuotationStatus.DRAFT, QuotationStatus.REJECTED, QuotationStatus.REVISED}


def generate_quotation_number() -> str:
    return "QTN" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-12:]


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
            coupon_disc = round(raw_disc, 2)
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
        quotation.tax_amount = round(taxable_amount * (quotation.tax_percent / 100.0), 2)
    quotation.total_amount = round(taxable_amount + quotation.tax_amount, 2)


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
        "approved_at": quotation.approved_at.isoformat() if quotation.approved_at else None,
        "rejection_reason": quotation.rejection_reason,
        "created_at": quotation.created_at.isoformat(),
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
    quotation.submitted_at = datetime.utcnow()
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
    quotation.approved_at = datetime.utcnow()
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
                    "created_at": item.created_at.isoformat(),
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
            total_price=round(unit_price * payload.quantity, 2),
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
        total_price=round(unit_price * payload.quantity, 2),
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
        total_price=round(payload.quantity * payload.unit_price, 2),
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
        part.total_price = round(part.quantity * part.unit_price, 2)
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
    item.total_price = round(item.unit_price * item.quantity, 2)
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
            cutoff = datetime.utcnow() - timedelta(days=30)
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
    # Category-smart: the service is pre-linked to the booking's service, which
    # is already category-filtered (AC booking → AC service category).
    try:
        existing_service_count = (await db.execute(
            sa_text("SELECT COUNT(*) FROM quotation_service_items WHERE quotation_id=:qid AND is_active=true"),
            {"qid": str(quotation_id)}
        )).scalar() or 0
        existing_appliance_count = (await db.execute(
            sa_text("SELECT COUNT(*) FROM quotation_appliances WHERE quotation_id=:qid AND is_active=true"),
            {"qid": str(quotation_id)}
        )).scalar() or 0
        # Only auto-add if this is the first appliance AND there are no service items yet
        if existing_appliance_count <= 1 and existing_service_count == 0:
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
                        if cp:
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
                    # Flush so the raw-SQL insert is visible to the ORM select inside _recalculate
                    await db.flush()
                    db.expire(quotation)  # force re-read of totals from DB
                    await _recalculate_quotation(db, quotation)
    except Exception as _auto_svc_err:
        import logging as _log2
        _log2.getLogger(__name__).warning("Auto-add booking service to quotation failed: %s", _auto_svc_err)
        # Non-fatal — don't fail the whole appliance-add operation

    await db.commit()
    _broadcast_quotation(quotation, WSEvent.QUOTATION_UPDATED, current_user["user_id"])

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
    confirmed_at = datetime.utcnow() if payload.is_repeat else None

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
    cutoff = datetime.utcnow() - timedelta(days=30)
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
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, SimpleDocTemplate, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
import logging as _logging
_pdf_logger = _logging.getLogger(__name__)


def _build_quotation_pdf(quotation, booking, customer, domain_profile, services, parts) -> bytes:
    """Builds a professional quotation PDF using ReportLab platypus."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
        title=f"Quotation {quotation.quotation_number}",
    )

    styles = getSampleStyleSheet()
    brand_color = rl_colors.HexColor("#1D4ED8")
    orange_color = rl_colors.HexColor("#F97316")
    ink_dark = rl_colors.HexColor("#111827")
    ink_mid = rl_colors.HexColor("#6B7280")
    ink_light = rl_colors.HexColor("#F3F4F6")

    h1 = ParagraphStyle("h1", fontSize=18, fontName="Helvetica-Bold", textColor=brand_color, leading=22)
    h2 = ParagraphStyle("h2", fontSize=12, fontName="Helvetica-Bold", textColor=ink_dark, leading=16)
    normal = ParagraphStyle("normal", fontSize=9, fontName="Helvetica", textColor=ink_dark, leading=14)
    small = ParagraphStyle("small", fontSize=8, fontName="Helvetica", textColor=ink_mid, leading=12)
    right = ParagraphStyle("right", fontSize=9, fontName="Helvetica", textColor=ink_dark, leading=14, alignment=TA_RIGHT)
    bold = ParagraphStyle("bold", fontSize=9, fontName="Helvetica-Bold", textColor=ink_dark, leading=14)
    total_style = ParagraphStyle("total", fontSize=11, fontName="Helvetica-Bold", textColor=brand_color, leading=16, alignment=TA_RIGHT)

    domain_name = domain_profile.get("business_name") if domain_profile else "Bibek Enterprises"
    domain_address = domain_profile.get("address", "") if domain_profile else ""
    domain_gstin = domain_profile.get("gstin", "") if domain_profile else ""
    domain_phone = domain_profile.get("support_phone", "") if domain_profile else ""
    domain_email = domain_profile.get("support_email", "") if domain_profile else ""
    domain_logo_url = domain_profile.get("logo_url", "") if domain_profile else ""

    cust_name = f"{customer.first_name or ''} {customer.last_name or ''}".strip() or "Customer"
    cust_mobile = customer.mobile_number or ""

    booking_no = booking.booking_number if booking else "—"
    service_name = booking.service_name if hasattr(booking, "service_name") and booking.service_name else "Service"

    status_label = {
        "DRAFT": "Draft",
        "SUBMITTED": "Pending Approval",
        "APPROVED": "Approved",
        "REJECTED": "Rejected",
        "REVISED": "Revised",
        "CONVERTED_TO_INVOICE": "Converted to Invoice",
        "EXPIRED": "Expired",
    }.get(str(quotation.status.value if hasattr(quotation.status, "value") else quotation.status), "Unknown")

    story = []

    # ── Header: logo (if available) + company name / QUOTATION label ──────
    # Try to fetch the logo and embed it; fall back to text-only if unavailable.
    logo_image = None
    if domain_logo_url:
        try:
            import urllib.request as _urlreq
            import tempfile as _tmp
            import os as _os
            from reportlab.platypus import Image as RLImage
            with _urlreq.urlopen(domain_logo_url, timeout=4) as _resp:
                _logo_data = _resp.read()
            _suffix = ".png" if "png" in domain_logo_url.lower() else ".jpg"
            _tmp_file = _tmp.NamedTemporaryFile(delete=False, suffix=_suffix)
            _tmp_file.write(_logo_data)
            _tmp_file.close()
            logo_image = RLImage(_tmp_file.name, width=36 * mm, height=18 * mm, kind="proportional")
        except Exception:
            logo_image = None

    # Build company info block (name + address + contact)
    company_lines = [f"<b>{domain_name or 'Bibek Enterprises'}</b>"]
    if domain_address:
        company_lines.append(domain_address)
    contact_parts = []
    if domain_phone:
        contact_parts.append(f"📞 {domain_phone}")
    if domain_email:
        contact_parts.append(f"✉ {domain_email}")
    if contact_parts:
        company_lines.append("  |  ".join(contact_parts))
    if domain_gstin:
        company_lines.append(f"GSTIN: {domain_gstin}")

    company_para = Paragraph("<br/>".join(company_lines), ParagraphStyle(
        "company", fontSize=9, fontName="Helvetica", textColor=ink_dark, leading=14,
    ))
    company_name_para = Paragraph(domain_name or "Bibek Enterprises", h1)

    quotation_label_para = Paragraph(
        f"QUOTATION<br/><font size=9 color='#6B7280'>{quotation.quotation_number}</font>",
        ParagraphStyle("qno", fontSize=18, fontName="Helvetica-Bold", textColor=orange_color, alignment=TA_RIGHT, leading=22),
    )

    if logo_image:
        header_data = [[logo_image, company_para, quotation_label_para]]
        header_table = Table(header_data, colWidths=["20%", "50%", "30%"])
    else:
        header_data = [[company_name_para, quotation_label_para]]
        header_table = Table(header_data, colWidths=["60%", "40%"])

    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(header_table)

    # Sub-header: address/contact lines if no logo (logo layout already includes them)
    if not logo_image:
        if domain_address:
            story.append(Paragraph(domain_address, small))
        contact_str = "  |  ".join(filter(None, [domain_phone, domain_email]))
        if contact_str:
            story.append(Paragraph(contact_str, small))
        if domain_gstin:
            story.append(Paragraph(f"GSTIN: {domain_gstin}", small))

    story.append(HRFlowable(width="100%", thickness=1, color=rl_colors.HexColor("#E5E7EB"), spaceAfter=8))

    # Info row
    created_str = ""
    try:
        import datetime as _dt
        if quotation.created_at:
            dt = quotation.created_at if hasattr(quotation.created_at, "strftime") else _dt.datetime.fromisoformat(str(quotation.created_at))
            created_str = dt.strftime("%-d %b %Y")
    except Exception:
        created_str = str(quotation.created_at or "")

    # Build address line for "Bill To"
    _addr_str = ""
    try:
        if booking and hasattr(booking, "address") and booking.address:
            _addr_str = str(booking.address)
    except Exception:
        pass

    # Scheduled date for booking info cell
    _sched_str = ""
    try:
        if booking and hasattr(booking, "scheduled_date") and booking.scheduled_date:
            import datetime as _dt2
            _sdt = booking.scheduled_date
            if hasattr(_sdt, "strftime"):
                _sched_str = _sdt.strftime("%-d %b %Y")
            else:
                _sched_str = str(_sdt)[:10]
    except Exception:
        pass

    bill_to_lines = [f"<b>Bill To</b>", cust_name, cust_mobile]
    if _addr_str:
        bill_to_lines.append(_addr_str[:80])  # truncate very long addresses

    booking_lines = [f"<b>Booking No.</b>", f"#{booking_no}", service_name]
    if _sched_str:
        booking_lines.append(f"Scheduled: {_sched_str}")

    info_data = [
        [
            Paragraph("<br/>".join(bill_to_lines), normal),
            Paragraph("<br/>".join(booking_lines), normal),
            Paragraph(f"<b>Date</b><br/>{created_str}<br/><b>Status:</b> {status_label}", normal),
        ]
    ]
    info_table = Table(info_data, colWidths=["33%", "33%", "34%"])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), ink_light),
        ("ROUNDEDCORNERS", [5]),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 10))

    # Line items table
    item_header = ["#", "Description", "Qty", "Unit Price", "Total"]
    item_rows = [item_header]
    idx = 1
    for s in services:
        item_rows.append([
            str(idx),
            s.service_name or s.custom_service_name or "—",
            str(s.quantity),
            f"\u20b9{s.unit_price:.0f}",
            f"\u20b9{s.total_price:.0f}",
        ])
        idx += 1
    for p in parts:
        item_rows.append([
            str(idx),
            f"{p.part_name} (Part)",
            str(p.quantity),
            f"\u20b9{p.unit_price:.0f}",
            f"\u20b9{p.total_price:.0f}",
        ])
        idx += 1

    col_widths = [8 * mm, None, 15 * mm, 30 * mm, 30 * mm]
    items_table = Table(item_rows, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand_color),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#F9FAFB")]),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 8))

    # Totals
    totals_data = []
    if float(quotation.discount_amount or 0) > 0:
        totals_data.append(["Discount:", f"-\u20b9{float(quotation.discount_amount):.0f}"])
    if float(quotation.adjustment_amount or 0) != 0:
        totals_data.append(["Adjustment:", f"\u20b9{float(quotation.adjustment_amount):.0f}"])
    totals_data.append([f"Tax ({float(quotation.tax_percent or 0):.0f}%):", f"\u20b9{float(quotation.tax_amount or 0):.0f}"])
    totals_data.append(["TOTAL AMOUNT:", f"\u20b9{float(quotation.total_amount or 0):.0f}"])

    totals_table = Table(
        [[Paragraph(r[0], right if i < len(totals_data) - 1 else ParagraphStyle("tl", fontSize=11, fontName="Helvetica-Bold", alignment=TA_RIGHT, textColor=ink_dark)),
          Paragraph(r[1], right if i < len(totals_data) - 1 else total_style)]
         for i, r in enumerate(totals_data)],
        colWidths=["75%", "25%"],
    )
    totals_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEABOVE", (0, -1), (-1, -1), 1, brand_color),
    ]))
    story.append(totals_table)

    if quotation.remarks:
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"<b>Notes:</b> {quotation.remarks}", small))

    # Footer
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor("#E5E7EB"), spaceAfter=6))

    footer_lines = [
        "This is a computer-generated quotation and does not require a physical signature.",
        "Prices are estimates and may vary based on actual parts used during repair.",
        "This quotation is valid for 7 days from the date of issue.",
    ]
    if domain_phone or domain_email:
        contact_info = "  |  ".join(filter(None, [domain_phone, domain_email]))
        footer_lines.append(f"For queries, contact us: {contact_info}")

    for line in footer_lines:
        story.append(Paragraph(line, small))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


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

    # Load domain profile for branding
    domain_profile = None
    try:
        domain = (await db.execute(select(Domain).where(Domain.id == booking.domain_id))).scalar_one_or_none()
        if domain:
            domain_profile = {
                "business_name": domain.business_name or domain.name,
                "address": domain.address or "",
                "gstin": domain.gstin or "",
                "support_phone": getattr(domain, "support_phone", None) or "",
                "support_email": getattr(domain, "support_email", None) or "",
                "logo_url": getattr(domain, "logo_url", None) or "",
            }
    except Exception:
        pass

    # Load services and parts line items
    services = (await db.execute(
        select(QuotationServiceItem).where(QuotationServiceItem.quotation_id == quotation.id)
    )).scalars().all()
    parts = (await db.execute(
        select(QuotationPartItem).where(QuotationPartItem.quotation_id == quotation.id)
    )).scalars().all()

    try:
        pdf_bytes = _build_quotation_pdf(quotation, booking, customer, domain_profile, services, parts)
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
