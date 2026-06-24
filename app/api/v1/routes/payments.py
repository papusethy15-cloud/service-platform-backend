from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from app.core.database import get_db
from app.api.deps import AnyAuthenticated
from app.api.v1.schemas.payment import (
    BankTransferPaymentRequest,
    CashPaymentRequest,
    CreateOrderRequest,
    GeneratePaymentLinkRequest,
    GeneratePaymentQRRequest,
    VerifyPaymentRequest,
)
from app.core.config import settings
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import PaymentMethod, PaymentStatus, PaymentTransaction
from app.models.technician import Technician
from app.models.user import User
from app.utils.response import success_response

router = APIRouter()

def generate_transaction_number() -> str:
    return "PAY" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-12:]


def generate_provider_order_id() -> str:
    return "ORD" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-12:]


async def _get_customer_id(db: AsyncSession, user_id: str):
    customer = (await db.execute(select(Customer).where(Customer.user_id == UUID(user_id)))).scalar_one_or_none()
    return customer.id if customer else None


async def _get_technician_id(db: AsyncSession, user_id: str):
    technician = (await db.execute(select(Technician).where(Technician.user_id == UUID(user_id)))).scalar_one_or_none()
    return technician.id if technician else None


async def _get_invoice_or_404(db: AsyncSession, invoice_id: UUID) -> Invoice:
    invoice = (await db.execute(select(Invoice).where(Invoice.id == invoice_id, Invoice.is_active == True))).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


async def _get_transaction_or_404(db: AsyncSession, transaction_id: UUID) -> PaymentTransaction:
    transaction = (
        await db.execute(select(PaymentTransaction).where(PaymentTransaction.id == transaction_id, PaymentTransaction.is_active == True))
    ).scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Payment transaction not found")
    return transaction


async def _ensure_technician_assigned(db: AsyncSession, invoice: Invoice):
    """Payments cannot be collected on a booking that has no technician assigned —
    if a quotation/invoice slipped through before the assignment guard existed,
    this still blocks the money-collection step."""
    booking = (await db.execute(select(Booking).where(Booking.id == invoice.booking_id))).scalar_one_or_none()
    if booking and not booking.technician_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot collect payment: no technician assigned to this booking. Assign a technician first."
        )


async def _ensure_invoice_access(db: AsyncSession, invoice: Invoice, current_user: dict):
    role = current_user["role"]
    if role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
        return
    booking = (await db.execute(select(Booking).where(Booking.id == invoice.booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if role == "CUSTOMER":
        customer_id = await _get_customer_id(db, current_user["user_id"])
        if not customer_id or booking.customer_id != customer_id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif role == "TECHNICIAN":
        technician_id = await _get_technician_id(db, current_user["user_id"])
        if booking.technician_id != technician_id:
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        raise HTTPException(status_code=403, detail="Access denied")


async def _apply_invoice_payment_state(db: AsyncSession, invoice: Invoice):
    paid_total = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                PaymentTransaction.invoice_id == invoice.id,
                PaymentTransaction.status == PaymentStatus.SUCCESS,
                PaymentTransaction.is_active == True,
            )
        )
    ).scalar_one()
    invoice.balance_amount = round(max(invoice.total_amount - paid_total, 0.0), 2)
    if paid_total <= 0:
        invoice.status = InvoiceStatus.GENERATED
    elif paid_total < invoice.total_amount:
        invoice.status = InvoiceStatus.PARTIALLY_PAID
    else:
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = datetime.utcnow()


def _payment_summary(transaction: PaymentTransaction, booking=None, customer_name: str = None, invoice_number: str = None):
    return {
        "id": str(transaction.id),
        "transaction_number": transaction.transaction_number,
        "invoice_id": str(transaction.invoice_id),
        "invoice_number": invoice_number,
        "booking_id": str(transaction.booking_id) if transaction.booking_id else None,
        "booking_number": booking.booking_number if booking else None,
        "customer_name": customer_name,
        "method": transaction.method.value if transaction.method else None,
        "payment_method": transaction.method.value if transaction.method else None,
        "status": transaction.status.value if transaction.status else None,
        "amount": transaction.amount,
        "currency": transaction.currency,
        "provider_order_id": transaction.provider_order_id,
        "provider_payment_id": transaction.provider_payment_id,
        "reference_number": transaction.reference_number,
        "payment_link": transaction.payment_link,
        "qr_payload": transaction.qr_payload,
        "notes": transaction.notes,
        "paid_at": transaction.paid_at.isoformat() if transaction.paid_at else None,
        "created_at": transaction.created_at.isoformat(),
    }


@router.post("/create-order", summary="Create Razorpay order")
async def create_order(
    payload: CreateOrderRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, UUID(payload.invoice_id))
    await _ensure_invoice_access(db, invoice, current_user)
    await _ensure_technician_assigned(db, invoice)
    amount = round(payload.amount if payload.amount is not None else invoice.balance_amount, 2)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")
    order_id = generate_provider_order_id()
    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        method=PaymentMethod.RAZORPAY,
        status=PaymentStatus.PENDING,
        amount=amount,
        provider_order_id=order_id,
        notes=payload.notes,
    )
    db.add(transaction)
    await db.flush()
    await db.commit()
    return success_response(
        data={
            "transaction_id": str(transaction.id),
            "order_id": order_id,
            "amount": amount,
            "currency": transaction.currency,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        },
        message="Payment order created successfully",
    )


@router.post("/verify", summary="Verify payment")
async def verify_payment(
    payload: VerifyPaymentRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    transaction = await _get_transaction_or_404(db, UUID(payload.transaction_id))
    invoice = await _get_invoice_or_404(db, transaction.invoice_id)
    await _ensure_invoice_access(db, invoice, current_user)
    transaction.provider_payment_id = payload.provider_payment_id
    transaction.provider_signature = payload.provider_signature
    transaction.status = PaymentStatus.SUCCESS
    transaction.verified_by = UUID(current_user["user_id"])
    transaction.paid_at = datetime.utcnow()
    if payload.amount is not None:
        transaction.amount = payload.amount
    if payload.notes:
        transaction.notes = payload.notes
    await _apply_invoice_payment_state(db, invoice)
    await db.commit()
    return success_response(data=_payment_summary(transaction), message="Payment verified successfully")


@router.post("/cash", summary="Cash payment")
async def cash_payment(
    payload: CashPaymentRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.payment import CashCollectionRecord, CashCollectionStatus

    invoice = await _get_invoice_or_404(db, UUID(payload.invoice_id))
    await _ensure_invoice_access(db, invoice, current_user)
    await _ensure_technician_assigned(db, invoice)

    # PAY_LATER: record as PENDING so it does NOT affect the invoice's paid balance.
    is_pay_later = (payload.reference_number or '').strip().upper() == 'PAY_LATER'
    txn_status   = PaymentStatus.PENDING if is_pay_later else PaymentStatus.SUCCESS
    txn_paid_at  = None if is_pay_later else datetime.utcnow()

    role = current_user["role"]
    # Determine cash_collection_status:
    # TECHNICIAN collecting cash → PENDING (needs to hand over to admin)
    # ADMIN/CCO/ACCOUNTANT collecting cash → COLLECTED (already in office hands)
    cash_coll_status = None
    if not is_pay_later:
        if role == "TECHNICIAN":
            cash_coll_status = CashCollectionStatus.PENDING
        elif role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
            cash_coll_status = CashCollectionStatus.COLLECTED

    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        verified_by=UUID(current_user["user_id"]) if not is_pay_later else None,
        method=PaymentMethod.CASH,
        status=txn_status,
        amount=payload.amount,
        reference_number=payload.reference_number,
        notes=payload.notes,
        paid_at=txn_paid_at,
        collected_by_role=role,
        cash_collection_status=cash_coll_status,
    )
    db.add(transaction)
    await db.flush()

    # Auto-create CashCollectionRecord when technician collects cash
    # so admin can track and acknowledge receipt
    if not is_pay_later and role == "TECHNICIAN":
        booking = (await db.execute(
            select(Booking).where(Booking.id == invoice.booking_id)
        )).scalar_one_or_none()
        if booking and booking.technician_id:
            ccr = CashCollectionRecord(
                payment_transaction_id=transaction.id,
                booking_id=invoice.booking_id,
                invoice_id=invoice.id,
                technician_id=booking.technician_id,
                customer_id=booking.customer_id,
                amount=payload.amount,
                status=CashCollectionStatus.PENDING,
            )
            db.add(ccr)

    # Only update invoice balance for real (non-PAY_LATER) payments
    if not is_pay_later:
        await _apply_invoice_payment_state(db, invoice)
    await db.commit()
    msg = "Pay Later scheduled successfully" if is_pay_later else "Cash payment recorded successfully"
    return success_response(data=_payment_summary(transaction), message=msg)


@router.post("/bank-transfer", summary="Bank payment")
async def bank_transfer_payment(
    payload: BankTransferPaymentRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, UUID(payload.invoice_id))
    await _ensure_invoice_access(db, invoice, current_user)
    await _ensure_technician_assigned(db, invoice)
    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        verified_by=UUID(current_user["user_id"]),
        method=PaymentMethod.BANK_TRANSFER,
        status=PaymentStatus.SUCCESS,
        amount=payload.amount,
        reference_number=payload.reference_number,
        notes=payload.notes,
        paid_at=datetime.utcnow(),
    )
    db.add(transaction)
    await db.flush()
    await _apply_invoice_payment_state(db, invoice)
    await db.commit()
    return success_response(data=_payment_summary(transaction), message="Bank transfer recorded successfully")


@router.post("/generate-link", summary="Payment link")
async def generate_payment_link(
    payload: GeneratePaymentLinkRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, UUID(payload.invoice_id))
    await _ensure_invoice_access(db, invoice, current_user)
    await _ensure_technician_assigned(db, invoice)
    amount = round(payload.amount if payload.amount is not None else invoice.balance_amount, 2)
    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        method=PaymentMethod.RAZORPAY,
        status=PaymentStatus.PENDING,
        amount=amount,
        provider_order_id=generate_provider_order_id(),
        payment_link=f"https://pay.palei.local/checkout/{invoice.id}?amount={amount}",
        notes=payload.notes,
    )
    db.add(transaction)
    await db.flush()
    await db.commit()
    return success_response(data=_payment_summary(transaction), message="Payment link generated successfully")


@router.post("/generate-qr", summary="UPI QR")
async def generate_payment_qr(
    payload: GeneratePaymentQRRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, UUID(payload.invoice_id))
    await _ensure_invoice_access(db, invoice, current_user)
    await _ensure_technician_assigned(db, invoice)
    amount = round(payload.amount if payload.amount is not None else invoice.balance_amount, 2)
    qr_payload = f"upi://pay?pa=palei@upi&pn=Palei%20Solutions&am={amount}&cu=INR&tn={invoice.invoice_number}"
    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        method=PaymentMethod.UPI,
        status=PaymentStatus.PENDING,
        amount=amount,
        qr_payload=qr_payload,
        notes=payload.notes,
    )
    db.add(transaction)
    await db.flush()
    await db.commit()
    return success_response(data=_payment_summary(transaction), message="Payment QR generated successfully")


@router.get("/history", summary="Transaction history")
async def payment_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    invoice_id: str = Query(None),
    booking_id: str = Query(None),
    method: str = Query(None),
    status: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime as dt
    from sqlalchemy import or_
    from app.models.invoice import Invoice

    query = (
        select(PaymentTransaction, Booking, Customer, Invoice)
        .outerjoin(Booking, Booking.id == PaymentTransaction.booking_id)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Invoice, Invoice.id == PaymentTransaction.invoice_id)
        .where(PaymentTransaction.is_active == True)
    )
    if invoice_id:
        query = query.where(PaymentTransaction.invoice_id == UUID(invoice_id))
    if booking_id:
        query = query.where(PaymentTransaction.booking_id == UUID(booking_id))
    if method:
        try:
            query = query.where(PaymentTransaction.method == PaymentMethod(method))
        except Exception:
            pass
    if status:
        try:
            query = query.where(PaymentTransaction.status == PaymentStatus(status))
        except Exception:
            pass
    if search:
        s = f"%{search}%"
        query = query.where(or_(
            PaymentTransaction.transaction_number.ilike(s),
            Booking.booking_number.ilike(s),
            Invoice.invoice_number.ilike(s),
        ))
    if date_from:
        try:
            query = query.where(PaymentTransaction.created_at >= dt.fromisoformat(date_from))
        except Exception:
            pass
    if date_to:
        try:
            query = query.where(PaymentTransaction.created_at <= dt.fromisoformat(date_to))
        except Exception:
            pass

    role = current_user["role"]
    if role == "CUSTOMER":
        customer_id = await _get_customer_id(db, current_user["user_id"])
        query = query.where(Booking.customer_id == customer_id)
    elif role == "TECHNICIAN":
        technician_id = await _get_technician_id(db, current_user["user_id"])
        query = query.where(Booking.technician_id == technician_id)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.order_by(PaymentTransaction.created_at.desc()).offset((page - 1) * per_page).limit(per_page))
    ).all()

    items = []
    for row in rows:
        txn, bk, cust, inv = row
        cust_name = cust.name if cust else None
        inv_number = inv.invoice_number if inv else None
        items.append(_payment_summary(txn, bk, cust_name, inv_number))

    return success_response(
        data={
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
    )
