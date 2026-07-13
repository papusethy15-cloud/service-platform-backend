"""
Cash Collection Routes
======================
When a technician collects cash from a customer, a CashCollectionRecord is created.
Admin/CCO can view pending collections per technician and mark them as collected.
A booking with pending cash collection cannot be settled (commission blocked).
"""
from app.utils.timezone import now_ist, now_naive
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.api.deps import AdminOrCCO, AnyAuthenticated
from app.models.payment import CashCollectionRecord, CashCollectionStatus
from app.models.booking import Booking
from app.models.invoice import Invoice
from app.models.technician import Technician
from app.models.customer import Customer
from app.models.user import User
from app.utils.response import success_response
from app.utils.notify import push_to_technician
from app.core.background_tasks import track_task

router = APIRouter()


def _fmt_collection(rec: CashCollectionRecord, technician=None, customer=None,
                    booking=None, invoice=None, collected_by_user=None) -> dict:
    return {
        "id": str(rec.id),
        "payment_transaction_id": str(rec.payment_transaction_id),
        "booking_id":    str(rec.booking_id),
        "booking_number": booking.booking_number if booking else None,
        "invoice_id":    str(rec.invoice_id),
        "invoice_number": invoice.invoice_number if invoice else None,
        "technician_id": str(rec.technician_id),
        "technician_name": technician.name if technician else None,
        "technician_code": technician.technician_code if technician else None,
        "customer_id":   str(rec.customer_id),
        "customer_name": customer.name if customer else None,
        "customer_mobile": customer.mobile if customer else None,
        "amount":  rec.amount,
        "status":  rec.status.value,
        "collected_by": str(rec.collected_by) if rec.collected_by else None,
        "collected_by_name": collected_by_user.name if collected_by_user else None,
        "collected_at": rec.collected_at.isoformat() if rec.collected_at else None,
        "notes":   rec.notes,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


@router.get("", summary="List cash collection records [Admin/CCO]")
async def list_cash_collections(
    technician_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="PENDING | COLLECTED"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(CashCollectionRecord, Technician, Customer, Booking, Invoice)
        .outerjoin(Technician, Technician.id == CashCollectionRecord.technician_id)
        .outerjoin(Customer,   Customer.id   == CashCollectionRecord.customer_id)
        .outerjoin(Booking,    Booking.id    == CashCollectionRecord.booking_id)
        .outerjoin(Invoice,    Invoice.id    == CashCollectionRecord.invoice_id)
        .where(CashCollectionRecord.is_active == True)
    )
    if technician_id:
        q = q.where(CashCollectionRecord.technician_id == UUID(technician_id))
    if status:
        try:
            q = q.where(CashCollectionRecord.status == CashCollectionStatus(status))
        except ValueError:
            pass

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(
            q.order_by(CashCollectionRecord.created_at.desc())
             .offset((page - 1) * per_page).limit(per_page)
        )
    ).all()

    items = []
    for rec, tech, cust, bk, inv in rows:
        items.append(_fmt_collection(rec, tech, cust, bk, inv))

    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@router.get("/summary", summary="Cash collection summary grouped by technician [Admin/CCO]")
async def cash_collection_summary(
    status: Optional[str] = Query("PENDING"),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns each technician with their total pending cash amount and record count.
    Used to display the technician selector on the collections page.
    """
    filter_status = CashCollectionStatus.PENDING
    if status == "COLLECTED":
        filter_status = CashCollectionStatus.COLLECTED

    rows = (
        await db.execute(
            select(
                CashCollectionRecord.technician_id,
                Technician.name,
                Technician.technician_code,
                Technician.mobile,
                func.count(CashCollectionRecord.id).label("record_count"),
                func.sum(CashCollectionRecord.amount).label("total_amount"),
            )
            .outerjoin(Technician, Technician.id == CashCollectionRecord.technician_id)
            .where(
                CashCollectionRecord.is_active == True,
                CashCollectionRecord.status == filter_status,
            )
            .group_by(
                CashCollectionRecord.technician_id,
                Technician.name,
                Technician.technician_code,
                Technician.mobile,
            )
            .order_by(func.sum(CashCollectionRecord.amount).desc())
        )
    ).all()

    return success_response(data=[{
        "technician_id":   str(r.technician_id),
        "technician_name": r.name,
        "technician_code": r.technician_code,
        "technician_mobile": r.mobile,
        "record_count": r.record_count,
        "total_amount": float(r.total_amount or 0),
    } for r in rows])


@router.get("/technician/{technician_id}", summary="Get all cash collections for a technician [Admin/CCO]")
async def collections_for_technician(
    technician_id: UUID,
    status: Optional[str] = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(CashCollectionRecord, Customer, Booking, Invoice)
        .outerjoin(Customer, Customer.id == CashCollectionRecord.customer_id)
        .outerjoin(Booking,  Booking.id  == CashCollectionRecord.booking_id)
        .outerjoin(Invoice,  Invoice.id  == CashCollectionRecord.invoice_id)
        .where(
            CashCollectionRecord.technician_id == technician_id,
            CashCollectionRecord.is_active == True,
        )
    )
    if status:
        try:
            q = q.where(CashCollectionRecord.status == CashCollectionStatus(status))
        except ValueError:
            pass

    rows = (await db.execute(q.order_by(CashCollectionRecord.created_at.desc()))).all()

    tech = (await db.execute(select(Technician).where(Technician.id == technician_id))).scalar_one_or_none()
    if not tech:
        raise HTTPException(404, "Technician not found")

    total_pending   = sum(r.CashCollectionRecord.amount for r in rows if r.CashCollectionRecord.status == CashCollectionStatus.PENDING)
    total_collected = sum(r.CashCollectionRecord.amount for r in rows if r.CashCollectionRecord.status == CashCollectionStatus.COLLECTED)

    items = []
    for rec, cust, bk, inv in rows:
        items.append(_fmt_collection(rec, tech, cust, bk, inv))

    return success_response(data={
        "technician": {
            "id": str(tech.id),
            "name": tech.name,
            "mobile": tech.mobile,
            "technician_code": tech.technician_code,
        },
        "total_pending":   total_pending,
        "total_collected": total_collected,
        "items": items,
    })


class MarkCollectedRequest(BaseModel):
    notes: Optional[str] = None


@router.post("/{collection_id}/collect", summary="Mark a cash collection as collected [Admin/CCO]")
async def mark_collected(
    collection_id: UUID,
    payload: MarkCollectedRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.payment import PaymentTransaction

    rec = (await db.execute(
        select(CashCollectionRecord).where(
            CashCollectionRecord.id == collection_id,
            CashCollectionRecord.is_active == True,
        )
    )).scalar_one_or_none()
    if not rec:
        raise HTTPException(404, "Cash collection record not found")
    if rec.status == CashCollectionStatus.COLLECTED:
        raise HTTPException(400, "Already marked as collected")

    rec.status       = CashCollectionStatus.COLLECTED
    rec.collected_by = UUID(current_user["user_id"])
    rec.collected_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE
    rec.notes        = payload.notes

    # Also update the payment transaction's cash_collection_status
    txn = (await db.execute(
        select(PaymentTransaction).where(PaymentTransaction.id == rec.payment_transaction_id)
    )).scalar_one_or_none()
    if txn:
        txn.cash_collection_status = CashCollectionStatus.COLLECTED

    await db.commit()

    tech  = (await db.execute(select(Technician).where(Technician.id == rec.technician_id))).scalar_one_or_none()
    cust  = (await db.execute(select(Customer).where(Customer.id == rec.customer_id))).scalar_one_or_none()
    bk    = (await db.execute(select(Booking).where(Booking.id == rec.booking_id))).scalar_one_or_none()
    inv   = (await db.execute(select(Invoice).where(Invoice.id == rec.invoice_id))).scalar_one_or_none()
    admin = (await db.execute(select(User).where(User.id == rec.collected_by))).scalar_one_or_none()

    # ── Notify technician ────────────────────────────────────────────────
    if tech:
        import asyncio
        bnum = bk.booking_number if bk else str(rec.booking_id)[:8]
        track_task(push_to_technician(
            db=db, technician=tech,
            title="Cash Collected 💰",
            body=f"Admin collected ₹{rec.amount:.2f} cash from you for booking {bnum}.",
            notif_type="PAYMENT",
            data={"type": "CASH_COLLECTED", "booking_id": str(rec.booking_id), "amount": str(rec.amount)},
        ))
    return success_response(
        data=_fmt_collection(rec, tech, cust, bk, inv, admin),
        message="Cash collection marked as collected"
    )


@router.post("/technician/{technician_id}/collect-all", summary="Mark ALL pending collections for a technician as collected [Admin/CCO]")
async def collect_all_for_technician(
    technician_id: UUID,
    payload: MarkCollectedRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.payment import PaymentTransaction

    pending = (await db.execute(
        select(CashCollectionRecord).where(
            CashCollectionRecord.technician_id == technician_id,
            CashCollectionRecord.status == CashCollectionStatus.PENDING,
            CashCollectionRecord.is_active == True,
        )
    )).scalars().all()

    if not pending:
        raise HTTPException(400, "No pending cash collections for this technician")

    now = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE (collected_at column)
    admin_id = UUID(current_user["user_id"])
    total = 0.0

    for rec in pending:
        rec.status       = CashCollectionStatus.COLLECTED
        rec.collected_by = admin_id
        rec.collected_at = now
        rec.notes        = payload.notes
        total += rec.amount

        txn = (await db.execute(
            select(PaymentTransaction).where(PaymentTransaction.id == rec.payment_transaction_id)
        )).scalar_one_or_none()
        if txn:
            txn.cash_collection_status = CashCollectionStatus.COLLECTED

    await db.commit()

    tech = (await db.execute(select(Technician).where(Technician.id == technician_id))).scalar_one_or_none()

    # ── Notify technician ─────────────────────────────────────────────────
    if tech:
        import asyncio
        track_task(push_to_technician(
            db=db, technician=tech,
            title="Cash Collected 💰",
            body=f"Admin collected all pending cash (₹{total:.2f}, {len(pending)} booking(s)) from you.",
            notif_type="PAYMENT",
            data={"type": "CASH_COLLECTED", "total": str(total), "records": str(len(pending))},
        ))
    return success_response(data={
        "technician_name": tech.name if tech else None,
        "records_collected": len(pending),
        "total_amount_collected": total,
    }, message=f"All {len(pending)} pending records marked as collected (₹{total:.2f})")
