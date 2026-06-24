from datetime import datetime
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from uuid import UUID
from reportlab.pdfgen import canvas
from app.core.database import get_db
from app.api.deps import AdminCCOTech, AnyAuthenticated
from app.api.v1.schemas.invoice import CreateInvoiceRequest, InvoiceSendRequest
from app.models.booking import Booking
from app.models.user import User
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.quotation import Quotation, QuotationStatus
from app.models.technician import Technician
from app.utils.response import success_response

router = APIRouter()

def generate_invoice_number() -> str:
    return "INV" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-12:]


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


async def _ensure_access(db: AsyncSession, invoice: Invoice, current_user: dict):
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


def _invoice_summary(invoice: Invoice, booking=None, customer_name: str = None, technician_name: str = None):
    return {
        "id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "booking_id": str(invoice.booking_id),
        "booking_number": booking.booking_number if booking else None,
        "quotation_id": str(invoice.quotation_id) if invoice.quotation_id else None,
        "invoice_type": invoice.invoice_type.value if invoice.invoice_type else None,
        "status": invoice.status.value if invoice.status else "GENERATED",
        "business_name": invoice.business_name,
        "business_address": invoice.business_address,
        "gstin": invoice.gstin,
        "taxable_amount": invoice.taxable_amount or 0,
        "cgst_amount": invoice.cgst_amount or 0,
        "sgst_amount": invoice.sgst_amount or 0,
        "igst_amount": invoice.igst_amount or 0,
        "gst_amount": round((invoice.cgst_amount or 0) + (invoice.sgst_amount or 0) + (invoice.igst_amount or 0), 2),
        "total_amount": invoice.total_amount or 0,
        "balance_amount": invoice.balance_amount or 0,
        "notes": invoice.notes,
        "pdf_url": invoice.pdf_url or f"/api/v1/invoices/{invoice.id}/pdf",
        "sent_email_at": invoice.sent_email_at.isoformat() if invoice.sent_email_at else None,
        "sent_whatsapp_at": invoice.sent_whatsapp_at.isoformat() if invoice.sent_whatsapp_at else None,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "created_at": invoice.created_at.isoformat(),
        "customer_name": customer_name,
        "technician_name": technician_name,
        "customer_phone": booking.customer_phone if booking and hasattr(booking, "customer_phone") else None,
        "coupon_code": booking.coupon_code if booking and hasattr(booking, "coupon_code") else None,
        "coupon_discount": booking.coupon_discount if booking and hasattr(booking, "coupon_discount") else 0.0,
    }


@router.post("", summary="Generate invoice")
async def create_invoice(
    payload: CreateInvoiceRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    quotation = (
        await db.execute(select(Quotation).where(Quotation.id == UUID(payload.quotation_id), Quotation.is_active == True))
    ).scalar_one_or_none()
    if not quotation:
        raise HTTPException(status_code=404, detail="Quotation not found")
    if quotation.status != QuotationStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only approved quotations can be converted to invoices")

    booking_for_check = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    if booking_for_check and not booking_for_check.technician_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot generate invoice: no technician assigned to this booking. Assign a technician first."
        )

    existing = (await db.execute(select(Invoice).where(Invoice.quotation_id == quotation.id, Invoice.is_active == True))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Invoice already exists for this quotation")

    invoice_type = InvoiceType(payload.invoice_type)
    taxable_amount = round(quotation.subtotal_amount - quotation.discount_amount + quotation.adjustment_amount, 2)
    total_tax = quotation.tax_amount if invoice_type != InvoiceType.NON_GST else 0.0
    cgst_amount = round(total_tax / 2, 2) if invoice_type != InvoiceType.NON_GST else 0.0
    sgst_amount = round(total_tax / 2, 2) if invoice_type != InvoiceType.NON_GST else 0.0
    igst_amount = 0.0

    if invoice_type == InvoiceType.GST_B2B and not (payload.gstin and payload.business_name and payload.business_address):
        raise HTTPException(status_code=400, detail="GST B2B invoices require GSTIN, business name, and business address")

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        booking_id=quotation.booking_id,
        quotation_id=quotation.id,
        generated_by=UUID(current_user["user_id"]),
        invoice_type=invoice_type,
        status=InvoiceStatus.GENERATED,
        business_name=payload.business_name,
        business_address=payload.business_address,
        gstin=payload.gstin,
        taxable_amount=taxable_amount,
        cgst_amount=cgst_amount,
        sgst_amount=sgst_amount,
        igst_amount=igst_amount,
        total_amount=round(taxable_amount + total_tax, 2),
        balance_amount=round(taxable_amount + total_tax, 2),
        notes=payload.notes,
    )
    db.add(invoice)

    quotation.status = QuotationStatus.CONVERTED_TO_INVOICE
    booking = (await db.execute(select(Booking).where(Booking.id == quotation.booking_id))).scalar_one_or_none()
    if booking:
        booking.base_amount = invoice.taxable_amount
        booking.gst_amount = invoice.cgst_amount + invoice.sgst_amount + invoice.igst_amount
        booking.total_amount = invoice.total_amount

    await db.flush()
    await db.commit()
    return success_response(data=_invoice_summary(invoice), message="Invoice generated successfully")


@router.get("", summary="Invoice list")
async def list_invoices(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    booking_id: str = Query(None),
    status: str = Query(None),
    invoice_type: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    # Build base query with joins
    base_query = (
        select(Invoice, Booking, Customer, Technician)
        .outerjoin(Booking, Booking.id == Invoice.booking_id)
        .outerjoin(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Technician, Technician.id == Booking.technician_id)
        .where(Invoice.is_active == True)
    )

    if booking_id:
        base_query = base_query.where(Invoice.booking_id == UUID(booking_id))
    if status:
        try:
            base_query = base_query.where(Invoice.status == InvoiceStatus(status))
        except Exception:
            pass
    if invoice_type:
        try:
            from app.models.invoice import InvoiceType as IT
            base_query = base_query.where(Invoice.invoice_type == IT(invoice_type))
        except Exception:
            pass
    if search:
        search_term = f"%{search}%"
        base_query = base_query.where(or_(
            Invoice.invoice_number.ilike(search_term),
            Booking.booking_number.ilike(search_term),
            Customer.name.ilike(search_term),
            Customer.mobile.ilike(search_term),
        ))
    if date_from:
        try:
            base_query = base_query.where(Invoice.created_at >= datetime.fromisoformat(date_from))
        except Exception:
            pass
    if date_to:
        try:
            base_query = base_query.where(Invoice.created_at <= datetime.fromisoformat(date_to))
        except Exception:
            pass

    role = current_user["role"]
    if role == "CUSTOMER":
        customer_id = await _get_customer_id(db, current_user["user_id"])
        base_query = base_query.where(Booking.customer_id == customer_id)
    elif role == "TECHNICIAN":
        technician_id = await _get_technician_id(db, current_user["user_id"])
        base_query = base_query.where(Booking.technician_id == technician_id)

    # Count using Invoice.id to avoid subquery issues with multi-column selects
    count_query = select(func.count(Invoice.id)).select_from(
        Invoice.__table__
        .outerjoin(Booking.__table__, Booking.id == Invoice.booking_id)
        .outerjoin(Customer.__table__, Customer.id == Booking.customer_id)
        .outerjoin(Technician.__table__, Technician.id == Booking.technician_id)
    ).where(Invoice.is_active == True)

    # Re-apply filters to count query
    if booking_id:
        count_query = count_query.where(Invoice.booking_id == UUID(booking_id))
    if status:
        try:
            count_query = count_query.where(Invoice.status == InvoiceStatus(status))
        except Exception:
            pass
    if invoice_type:
        try:
            from app.models.invoice import InvoiceType as IT
            count_query = count_query.where(Invoice.invoice_type == IT(invoice_type))
        except Exception:
            pass
    if search:
        search_term = f"%{search}%"
        count_query = count_query.where(or_(
            Invoice.invoice_number.ilike(search_term),
            Booking.booking_number.ilike(search_term),
            Customer.name.ilike(search_term),
            Customer.mobile.ilike(search_term),
        ))
    if date_from:
        try:
            count_query = count_query.where(Invoice.created_at >= datetime.fromisoformat(date_from))
        except Exception:
            pass
    if date_to:
        try:
            count_query = count_query.where(Invoice.created_at <= datetime.fromisoformat(date_to))
        except Exception:
            pass
    if role == "CUSTOMER":
        count_query = count_query.where(Booking.customer_id == customer_id)
    elif role == "TECHNICIAN":
        count_query = count_query.where(Booking.technician_id == technician_id)

    total = (await db.execute(count_query)).scalar_one()
    rows = (await db.execute(
        base_query.order_by(Invoice.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).all()

    items = []
    for row in rows:
        inv, bk, cust, tech = row
        cust_name = cust.name if cust else None
        tech_name = None
        if tech and tech.user_id:
            user_row = (await db.execute(select(User).where(User.id == tech.user_id))).scalar_one_or_none()
            tech_name = user_row.name if user_row else None
        items.append(_invoice_summary(inv, bk, cust_name, tech_name))

    return success_response(
        data={
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if total > 0 else 1,
        }
    )


@router.get("/{invoice_id}", summary="Invoice details")
async def get_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id)
    await _ensure_access(db, invoice, current_user)
    return success_response(data=_invoice_summary(invoice))


@router.get("/{invoice_id}/pdf", summary="Download PDF")
async def get_invoice_pdf(
    invoice_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id)
    await _ensure_access(db, invoice, current_user)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.setTitle(invoice.invoice_number)
    pdf.drawString(50, 800, f"Invoice: {invoice.invoice_number}")
    pdf.drawString(50, 780, f"Type: {invoice.invoice_type.value}")
    pdf.drawString(50, 760, f"Booking ID: {invoice.booking_id}")
    pdf.drawString(50, 740, f"Quotation ID: {invoice.quotation_id}")
    pdf.drawString(50, 720, f"Taxable Amount: INR {invoice.taxable_amount:.2f}")
    pdf.drawString(50, 700, f"CGST: INR {invoice.cgst_amount:.2f}")
    pdf.drawString(50, 680, f"SGST: INR {invoice.sgst_amount:.2f}")
    pdf.drawString(50, 660, f"IGST: INR {invoice.igst_amount:.2f}")
    pdf.drawString(50, 640, f"Total Amount: INR {invoice.total_amount:.2f}")
    pdf.drawString(50, 620, f"Balance Amount: INR {invoice.balance_amount:.2f}")
    pdf.drawString(50, 600, f"Generated On: {invoice.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={invoice.invoice_number}.pdf"},
    )


@router.post("/{invoice_id}/email", summary="Send email")
async def send_invoice_email(
    invoice_id: UUID,
    payload: InvoiceSendRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id)
    invoice.sent_email_at = datetime.utcnow()
    await db.commit()
    return success_response(
        data={"invoice_id": str(invoice.id), "recipient": payload.recipient, "sent_at": invoice.sent_email_at.isoformat()},
        message="Invoice email queued successfully",
    )


@router.post("/{invoice_id}/whatsapp", summary="Send WhatsApp")
async def send_invoice_whatsapp(
    invoice_id: UUID,
    payload: InvoiceSendRequest,
    current_user: dict = Depends(AdminCCOTech),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id)
    invoice.sent_whatsapp_at = datetime.utcnow()
    await db.commit()
    return success_response(
        data={"invoice_id": str(invoice.id), "recipient": payload.recipient, "sent_at": invoice.sent_whatsapp_at.isoformat()},
        message="Invoice WhatsApp queued successfully",
    )
