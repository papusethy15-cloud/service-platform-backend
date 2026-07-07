import hashlib
import hmac
import logging
from datetime import datetime
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
import razorpay
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
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
from app.models.domain import DomainProfile
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import PaymentMethod, PaymentStatus, PaymentTransaction
from app.models.system_setting import SystemSetting
from app.models.technician import Technician
from app.models.user import User
from app.utils.response import success_response

router = APIRouter()
logger = logging.getLogger(__name__)


async def _get_razorpay_keys(db: AsyncSession) -> tuple[str, str]:
    """
    Reads the live Razorpay key id/secret from the admin-managed
    system_settings table (group="payment") — the same single-source-of-
    truth pattern used for the Google Maps key. Falls back to the static
    .env values only if nothing has been configured via the admin
    dashboard yet.
    """
    rows = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.group == "payment",
                SystemSetting.key.in_(["razorpay_key_id", "razorpay_key_secret"]),
            )
        )
    ).scalars().all()
    values = {row.key: row.value for row in rows if row.value}
    key_id = values.get("razorpay_key_id") or settings.RAZORPAY_KEY_ID
    key_secret = values.get("razorpay_key_secret") or settings.RAZORPAY_KEY_SECRET
    return key_id, key_secret

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
        "due_collect_at": transaction.due_collect_at.isoformat() if getattr(transaction, "due_collect_at", None) else None,
        "last_reminder_at": transaction.last_reminder_at.isoformat() if getattr(transaction, "last_reminder_at", None) else None,
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

    key_id, key_secret = await _get_razorpay_keys(db)
    if not key_id or not key_secret:
        raise HTTPException(
            status_code=400,
            detail="Razorpay is not configured. Add the key ID and secret in admin dashboard Settings → Payment.",
        )

    # ── Real Razorpay Orders API call ────────────────────────────────────
    # Amount must be in the smallest currency unit (paise for INR).
    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        rp_order = client.order.create({
            "amount": int(round(amount * 100)),
            "currency": "INR",
            "receipt": f"inv_{invoice.invoice_number}",
            "payment_capture": 1,
            "notes": {"invoice_id": str(invoice.id), "booking_id": str(invoice.booking_id)},
        })
    except Exception as e:
        logger.error(f"[Razorpay] order.create failed: {e}")
        raise HTTPException(status_code=502, detail=f"Could not create Razorpay order: {e}")

    order_id = rp_order["id"]
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
            "razorpay_key_id": key_id,
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

    # ── Verify the Razorpay HMAC-SHA256 signature server-side ────────────
    # Only RAZORPAY transactions carry a provider signature to check; cash/
    # bank/manual methods never reach this endpoint via the same trust path.
    if transaction.method == PaymentMethod.RAZORPAY:
        if not payload.provider_signature:
            raise HTTPException(status_code=400, detail="Missing payment signature")
        _, key_secret = await _get_razorpay_keys(db)
        if not key_secret:
            raise HTTPException(status_code=400, detail="Razorpay is not configured")
        expected_signature = hmac.new(
            key_secret.encode("utf-8"),
            f"{transaction.provider_order_id}|{payload.provider_payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, payload.provider_signature):
            transaction.status = PaymentStatus.FAILED
            transaction.provider_payment_id = payload.provider_payment_id
            transaction.provider_signature = payload.provider_signature
            transaction.notes = "Signature verification failed"
            await db.commit()
            logger.warning(f"[Razorpay] signature mismatch for transaction {transaction.id}")
            raise HTTPException(status_code=400, detail="Payment signature verification failed")

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

    # PAY_LATER is a deferred collection schedule — no technician needs to be
    # on-site yet, so skip the assignment guard for pay-later requests.
    _early_is_pay_later = bool(getattr(payload, "is_pay_later", False)) or \
        (payload.reference_number or '').strip().upper() == 'PAY_LATER'
    if not _early_is_pay_later:
        await _ensure_technician_assigned(db, invoice)

    # ── Idempotency guard: block double-payment on an already-fully-paid invoice ──
    # This is the critical guard that prevents both technician AND CCO from each
    # recording a separate cash collection for the same invoice (double-counting).
    # We re-read balance_amount from DB (set by _apply_invoice_payment_state) which
    # is always the authoritative source. PAY_LATER records are exempt — they are
    # PENDING and do not reduce balance_amount, so scheduling a reminder is safe.
    is_pay_later_check = bool(getattr(payload, "is_pay_later", False)) or         (payload.reference_number or '').strip().upper() == 'PAY_LATER'
    if not is_pay_later_check and invoice.balance_amount is not None and invoice.balance_amount <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invoice {invoice.invoice_number} is already fully paid "
                f"(balance ₹{invoice.balance_amount:.2f}). "
                "No further payment can be recorded on this invoice."
            ),
        )

    # PAY_LATER: record as PENDING so it does NOT affect the invoice's paid balance.
    # Prefer the explicit is_pay_later flag; fall back to the legacy
    # reference_number == "PAY_LATER" sentinel for any callers not yet updated.
    is_pay_later = bool(getattr(payload, "is_pay_later", False)) or \
        (payload.reference_number or '').strip().upper() == 'PAY_LATER'
    due_collect_at = getattr(payload, "due_collect_at", None) if is_pay_later else None
    if is_pay_later and not due_collect_at:
        raise HTTPException(status_code=400, detail="due_collect_at is required when is_pay_later is set — pick the date/time to remind for collection.")
    txn_status   = PaymentStatus.PENDING if is_pay_later else PaymentStatus.SUCCESS
    txn_paid_at  = None if is_pay_later else datetime.utcnow()
    txn_method   = PaymentMethod.PAY_LATER if is_pay_later else PaymentMethod.CASH

    role = current_user["role"]

    # ── Determine if this is "admin acting on behalf of technician" ────────────
    # When admin passes on_behalf_technician_id (a user id), we look up the
    # Technician record for that user and treat the collection as if the
    # technician did it: PENDING cash that still needs deposit to admin.
    on_behalf_tech_user_id = getattr(payload, "on_behalf_technician_id", None)
    acting_as_technician = False
    on_behalf_technician_record = None

    if not is_pay_later and on_behalf_tech_user_id and role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
        from app.models.technician import Technician as TechModel
        # on_behalf_technician_id can be either:
        #   a) Technician.id  (what booking.technician_id returns — preferred from admin dashboard)
        #   b) Technician.user_id (legacy / alternate)
        # Try by Technician.id first, then fall back to user_id.
        on_behalf_technician_record = (await db.execute(
            select(TechModel).where(TechModel.id == UUID(on_behalf_tech_user_id), TechModel.is_active == True)
        )).scalar_one_or_none()
        if not on_behalf_technician_record:
            on_behalf_technician_record = (await db.execute(
                select(TechModel).where(TechModel.user_id == UUID(on_behalf_tech_user_id), TechModel.is_active == True)
            )).scalar_one_or_none()
        if on_behalf_technician_record:
            acting_as_technician = True

    # Determine cash_collection_status:
    # TECHNICIAN collecting cash → PENDING (needs to hand over to admin)
    # ADMIN acting on behalf of technician → PENDING (same — cash is with tech)
    # ADMIN/CCO/ACCOUNTANT collecting directly → COLLECTED (already in office)
    cash_coll_status = None
    if not is_pay_later:
        if role == "TECHNICIAN" or acting_as_technician:
            cash_coll_status = CashCollectionStatus.PENDING
        elif role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
            cash_coll_status = CashCollectionStatus.COLLECTED

    effective_role = "TECHNICIAN" if acting_as_technician else role

    transaction = PaymentTransaction(
        transaction_number=generate_transaction_number(),
        invoice_id=invoice.id,
        booking_id=invoice.booking_id,
        created_by=UUID(current_user["user_id"]),
        verified_by=UUID(current_user["user_id"]) if not is_pay_later else None,
        method=txn_method,
        status=txn_status,
        amount=payload.amount,
        reference_number=payload.reference_number,
        notes=payload.notes,
        paid_at=txn_paid_at,
        collected_by_role=effective_role,
        cash_collection_status=cash_coll_status,
        due_collect_at=due_collect_at,
    )
    db.add(transaction)
    await db.flush()

    # ── Auto-void any stale PENDING PAY_LATER records for this invoice ──────
    # When a real payment (CASH / UPI / BANK_TRANSFER / RAZORPAY) is collected,
    # any previously scheduled PAY_LATER PENDING transaction for the same invoice
    # is now obsolete — the customer has already paid. Mark them CANCELLED so
    # they no longer appear as pending ghost records on the CCO dashboard.
    if not is_pay_later:
        stale_pay_later = (await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.invoice_id == invoice.id,
                PaymentTransaction.method    == PaymentMethod.PAY_LATER,
                PaymentTransaction.status    == PaymentStatus.PENDING,
                PaymentTransaction.is_active == True,
            )
        )).scalars().all()
        for stale in stale_pay_later:
            stale.status    = PaymentStatus.CANCELLED   # type: ignore[attr-defined]
            stale.notes     = ((stale.notes or '') + ' [Auto-voided: real payment collected]')[:1000]
            stale.is_active = False   # soft-delete so it disappears from active lists

    # Auto-create CashCollectionRecord when:
    #   a) technician themselves collected cash, OR
    #   b) admin collected cash on behalf of a technician
    # In both cases the cash is physically with the technician and needs deposit.
    create_ccr = not is_pay_later and (role == "TECHNICIAN" or acting_as_technician)
    if create_ccr:
        booking = (await db.execute(
            select(Booking).where(Booking.id == invoice.booking_id)
        )).scalar_one_or_none()

        # Resolve which technician record owns this cash
        if acting_as_technician and on_behalf_technician_record:
            tech_record_id = on_behalf_technician_record.id
            cust_id = booking.customer_id if booking else None
        elif booking and booking.technician_id:
            tech_record_id = booking.technician_id
            cust_id = booking.customer_id
        else:
            tech_record_id = None
            cust_id = None

        if tech_record_id and cust_id:
            ccr = CashCollectionRecord(
                payment_transaction_id=transaction.id,
                booking_id=invoice.booking_id,
                invoice_id=invoice.id,
                technician_id=tech_record_id,
                customer_id=cust_id,
                amount=payload.amount,
                status=CashCollectionStatus.PENDING,
            )
            db.add(ccr)

    # Only update invoice balance for real (non-PAY_LATER) payments
    if not is_pay_later:
        await _apply_invoice_payment_state(db, invoice)
    # Collect the summary BEFORE commit — after commit SQLAlchemy expires all
    # ORM attributes, and accessing them in an async context (without await)
    # raises MissingGreenlet / DetachedInstanceError.
    # We need the booking for the summary (booking_number), so fetch it now.
    _summary_booking = (await db.execute(
        select(Booking).where(Booking.id == invoice.booking_id)
    )).scalar_one_or_none()
    summary_data = _payment_summary(transaction, booking=_summary_booking)
    try:
        await db.commit()
    except Exception as db_err:
        logger.error(
            f"[cash_payment] DB commit failed for invoice {payload.invoice_id}: {db_err}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Database error while recording payment: {db_err}")
    msg = "Pay Later scheduled successfully" if is_pay_later else "Cash payment recorded successfully"
    logger.info(f"[cash_payment] {msg} — invoice {invoice.invoice_number} amount={payload.amount}")
    return success_response(data=summary_data, message=msg)


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
    exclude_status: str = Query(None),
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
    if exclude_status:
        try:
            ex = [PaymentStatus(s.strip()) for s in exclude_status.split(",") if s.strip()]
            query = query.where(PaymentTransaction.status.notin_(ex))
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


@router.post("/{transaction_id}/mark-collected", summary="Mark PAY_LATER as collected [CCO/Admin]")
async def mark_pay_later_collected(
    transaction_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    CCO action: a PAY_LATER PENDING transaction that has now been collected
    (cash received in office, or confirmed paid via other channel).
    Marks the transaction SUCCESS, sets paid_at, and re-applies invoice state.
    Also voids any other stale PAY_LATER PENDING records on the same invoice.
    """
    from app.models.payment import CashCollectionStatus
    role = current_user["role"]
    if role not in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    txn = (await db.execute(
        select(PaymentTransaction).where(PaymentTransaction.id == transaction_id, PaymentTransaction.is_active == True)
    )).scalar_one_or_none()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.method != PaymentMethod.PAY_LATER:
        raise HTTPException(status_code=400, detail="Only PAY_LATER transactions can use this action")
    if txn.status != PaymentStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Transaction is already {txn.status.value}")

    txn.status = PaymentStatus.SUCCESS
    txn.method = PaymentMethod.CASH  # collected as cash
    txn.paid_at = datetime.utcnow()
    txn.verified_by = UUID(current_user["user_id"])
    txn.cash_collection_status = CashCollectionStatus.COLLECTED
    txn.notes = (txn.notes or "") + f" [Marked collected by CCO {current_user['user_id']} on {datetime.utcnow().date()}]"

    invoice = await _get_invoice_or_404(db, txn.invoice_id)

    # Void any other stale PAY_LATER PENDING on same invoice
    stale = (await db.execute(
        select(PaymentTransaction).where(
            PaymentTransaction.invoice_id == txn.invoice_id,
            PaymentTransaction.id != txn.id,
            PaymentTransaction.method == PaymentMethod.PAY_LATER,
            PaymentTransaction.status == PaymentStatus.PENDING,
            PaymentTransaction.is_active == True,
        )
    )).scalars().all()
    for s in stale:
        s.status = PaymentStatus.CANCELLED  # type: ignore[attr-defined]
        s.notes = ((s.notes or "") + " [Auto-voided: payment collected via mark-collected action]")[:1000]
        s.is_active = False

    await _apply_invoice_payment_state(db, invoice)
    await db.commit()
    return success_response(
        data={"id": str(txn.id), "status": txn.status.value, "paid_at": txn.paid_at.isoformat()},
        message="PAY_LATER marked as collected successfully"
    )


@router.post("/{transaction_id}/void", summary="Void stale PAY_LATER transaction [CCO/Admin]")
async def void_pay_later(
    transaction_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """
    CCO action: void an orphaned PAY_LATER PENDING transaction where payment
    was already collected via another method (cash, UPI, etc.).
    Soft-deletes the record so it no longer appears on the dashboard.
    """
    role = current_user["role"]
    if role not in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    txn = (await db.execute(
        select(PaymentTransaction).where(PaymentTransaction.id == transaction_id, PaymentTransaction.is_active == True)
    )).scalar_one_or_none()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.method != PaymentMethod.PAY_LATER:
        raise HTTPException(status_code=400, detail="Only PAY_LATER transactions can be voided here")
    if txn.status != PaymentStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Transaction is already {txn.status.value}")

    txn.status = PaymentStatus.CANCELLED  # type: ignore[attr-defined]
    txn.is_active = False
    txn.notes = ((txn.notes or "") + f" [Voided by CCO {current_user['user_id']} on {datetime.utcnow().date()} — payment already collected via other method]")[:1000]

    await db.commit()
    return success_response(
        data={"id": str(txn.id), "status": "CANCELLED"},
        message="PAY_LATER transaction voided successfully"
    )


@router.get("/{transaction_id}/receipt", summary="Download payment receipt PDF")
async def get_payment_receipt_pdf(
    transaction_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """A short, branded payment slip/receipt — separate from the full tax
    invoice — confirming a specific transaction was received."""
    transaction = await _get_transaction_or_404(db, transaction_id)
    invoice = await _get_invoice_or_404(db, transaction.invoice_id)
    await _ensure_invoice_access(db, invoice, current_user)

    booking = (await db.execute(select(Booking).where(Booking.id == invoice.booking_id))).scalar_one_or_none()
    customer = None
    if booking:
        customer = (await db.execute(select(Customer).where(Customer.id == booking.customer_id))).scalar_one_or_none()
    domain_profile = None
    domain_id = invoice.domain_id or (booking.domain_id if booking else None)
    if domain_id:
        domain_profile = (await db.execute(
            select(DomainProfile).where(DomainProfile.domain_id == domain_id)
        )).scalar_one_or_none()

    business_name = invoice.business_name or (domain_profile.business_legal_name if domain_profile else None) or "Palei Solutions"

    buffer = BytesIO()
    try:
        BRAND = colors.HexColor("#1E3A8A")
        ACCENT = colors.HexColor("#F97316")
        DARK = colors.HexColor("#111827")
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
            title=f"Receipt-{transaction.transaction_number}",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("Title", parent=styles["Heading1"], fontSize=16, textColor=BRAND, alignment=TA_CENTER)
        center_small = ParagraphStyle("CenterSmall", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#6B7280"), alignment=TA_CENTER)
        label = ParagraphStyle("Label", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#6B7280"))
        value = ParagraphStyle("Value", parent=styles["Normal"], fontSize=10, textColor=DARK, alignment=TA_RIGHT)

        elements = [
            Paragraph(business_name, title_style),
            Spacer(1, 2 * mm),
            Paragraph("PAYMENT RECEIPT", center_small),
            Spacer(1, 6 * mm),
        ]
        rule = Table([[""]], colWidths=[170 * mm], rowHeights=[1.2])
        rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]))
        elements.append(rule)
        elements.append(Spacer(1, 6 * mm))

        rows = [
            ("Receipt No.", transaction.transaction_number),
            ("Invoice No.", invoice.invoice_number),
            ("Customer", customer.name if customer else "-"),
            ("Payment Method", transaction.method.value if transaction.method else "-"),
            ("Payment ID", transaction.provider_payment_id or "-"),
            ("Status", transaction.status.value if transaction.status else "-"),
            ("Paid On", transaction.paid_at.strftime("%d %b %Y, %I:%M %p") if transaction.paid_at else "-"),
        ]
        table_data = [[Paragraph(k, label), Paragraph(v, value)] for k, v in rows]
        info_table = Table(table_data, colWidths=[70 * mm, 100 * mm])
        info_table.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 8 * mm))

        amount_table = Table(
            [[Paragraph("Amount Paid", ParagraphStyle("AmtLabel", parent=styles["Normal"], fontSize=12, textColor=DARK)),
              Paragraph(f"INR {transaction.amount:.2f}", ParagraphStyle("AmtValue", parent=styles["Normal"], fontSize=14, textColor=ACCENT, alignment=TA_RIGHT))]],
            colWidths=[85 * mm, 85 * mm],
        )
        amount_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))
        elements.append(amount_table)
        elements.append(Spacer(1, 10 * mm))
        elements.append(Paragraph("This is a computer-generated receipt and does not require a signature.", center_small))

        doc.build(elements)
        buffer.seek(0)
        pdf_bytes = buffer.read()
    except Exception:
        logger.exception("Receipt PDF generation failed, falling back to plain layout")
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(50, 800, f"Receipt: {transaction.transaction_number}")
        pdf.drawString(50, 780, f"Amount: INR {transaction.amount:.2f}")
        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        pdf_bytes = buffer.read()

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=receipt_{transaction.transaction_number}.pdf"},
    )
