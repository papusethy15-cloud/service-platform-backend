from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text as sa_text
from uuid import UUID
from app.core.database import get_db
from app.api.deps import AdminCCOTech, AnyAuthenticated
from app.api.v1.schemas.quotation import (
    AddQuotationPartRequest,
    AddQuotationServiceRequest,
    ApplyAdjustmentRequest,
    ApplyDiscountRequest,
    CreateQuotationRequest,
    QuotationActionRequest,
    UpdateQuotationPartRequest,
    UpdateQuotationRequest,
)
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.quotation import (
    PartSource,
    Quotation,
    QuotationPartItem,
    QuotationServiceItem,
    QuotationStatus,
    QuotationStatusLog,
)
from app.models.service import Service
from app.models.technician import Technician
from app.models.domain import Domain
from app.utils.response import success_response

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
    data["services"] = [
        {
            "id": str(item.id),
            "service_id": str(item.service_id),
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
    return success_response(data=_quotation_summary(quotation), message="Quotation reverted to DRAFT")

@router.post("/{quotation_id}/submit", summary="Submit quotation")
async def submit_quotation(
    quotation_id: UUID,
    payload: QuotationActionRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not in a submittable state")
    quotation.status = QuotationStatus.SUBMITTED
    quotation.submitted_at = datetime.utcnow()
    await _add_status_log(db, quotation, current_user["user_id"], payload.notes or "Quotation submitted")
    await db.commit()
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
    if booking:
        booking.base_amount = round(quotation.subtotal_amount - quotation.discount_amount + quotation.adjustment_amount, 2)
        booking.gst_amount = quotation.tax_amount
        booking.total_amount = quotation.total_amount
    await _add_status_log(db, quotation, current_user["user_id"], payload.notes or "Quotation approved")
    await db.commit()
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


@router.post("/{quotation_id}/services", summary="Add service")
async def add_service_to_quotation(
    quotation_id: UUID,
    payload: AddQuotationServiceRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = await _get_quotation_or_404(db, quotation_id)
    if quotation.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Quotation is not editable")
    service = (await db.execute(select(Service).where(Service.id == UUID(payload.service_id), Service.is_active == True))).scalar_one_or_none()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    unit_price = payload.unit_price if payload.unit_price is not None else service.base_price
    # Encode appliance_label into service_name so UI can group by appliance
    encoded_name = f"{payload.appliance_label} :: {service.name}" if payload.appliance_label else service.name
    item = QuotationServiceItem(
        quotation_id=quotation.id,
        service_id=service.id,
        service_name=encoded_name,
        quantity=payload.quantity,
        unit_price=unit_price,
        total_price=round(unit_price * payload.quantity, 2),
    )
    db.add(item)
    await db.flush()
    await _recalculate_quotation(db, quotation)
    await db.commit()
    return success_response(data={"id": str(item.id), "total_price": item.total_price}, message="Service added successfully")


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
    await db.commit()

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
    return success_response(data={
        "coupons_fixed": fixed_coupons,
        "quotations_repaired": repaired_quotations,
    }, message=f"Repair complete: {len(fixed_coupons)} coupon counts fixed, {len(repaired_quotations)} quotations repaired")
