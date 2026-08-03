import asyncio
import logging

from app.utils.timezone import now_ist, ist_invoice_suffix, now_naive
from datetime import datetime
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from uuid import UUID
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether, HRFlowable,
)
from reportlab.lib.utils import ImageReader
from app.core.database import get_db
from app.api.deps import AdminCCOTech, AnyAuthenticated
from app.api.v1.schemas.invoice import CreateInvoiceRequest, InvoiceSendRequest
from app.models.booking import Booking
from app.models.user import User
from app.models.customer import Customer
from app.models.domain import Domain, DomainProfile
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.quotation import Quotation, QuotationStatus, QuotationServiceItem, QuotationPartItem
from app.models.technician import Technician
from app.models.payment import PaymentTransaction, PaymentMethod, PaymentStatus
from app.utils.response import success_response, iso

router = APIRouter()
logger = logging.getLogger(__name__)

def generate_invoice_number() -> str:
    return "INV" + ist_invoice_suffix()[-12:]


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


def _invoice_summary(invoice: Invoice, booking=None, customer_name: str = None, technician_name: str = None, has_pay_later: bool = False):
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
        "gst_amount": int(round((invoice.cgst_amount or 0) + (invoice.sgst_amount or 0) + (invoice.igst_amount or 0))),
        "total_amount": invoice.total_amount or 0,
        "balance_amount": invoice.balance_amount or 0,
        "notes": invoice.notes,
        "pdf_url": invoice.pdf_url or f"/api/v1/invoices/{invoice.id}/pdf",
        "sent_email_at": iso(invoice.sent_email_at) if invoice.sent_email_at else None,
        "sent_whatsapp_at": iso(invoice.sent_whatsapp_at) if invoice.sent_whatsapp_at else None,
        "paid_at": iso(invoice.paid_at) if invoice.paid_at else None,
        "created_at": iso(invoice.created_at),
        "customer_name": customer_name,
        "technician_name": technician_name,
        "customer_phone": booking.customer_phone if booking and hasattr(booking, "customer_phone") else None,
        "coupon_code": booking.coupon_code if booking and hasattr(booking, "coupon_code") else None,
        "coupon_discount": booking.coupon_discount if booking and hasattr(booking, "coupon_discount") else 0.0,
        "has_pay_later": has_pay_later,
        # Aliases expected by the website InvoiceCard component
        "payment_status": invoice.status.value if invoice.status else "GENERATED",
        "balance_due": invoice.balance_amount or 0,
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

    # ── Invoice type is derived from the quotation's tax_mode.
    #    Technicians CAN generate invoices for any approved quotation, including
    #    Non-GST ones (tax_mode = NONE) that admin may have set on the quotation.
    #    The restriction is only that technicians cannot CREATE Non-GST quotations.
    role = current_user["role"]
    q_tax_mode = (getattr(quotation, "tax_mode", "B2C") or "B2C").upper()
    is_office_role = role in {"SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT"}

    if is_office_role:
        # Admin/office staff retain full control, including explicit invoice_type override.
        invoice_type = InvoiceType(payload.invoice_type)
        business_name = payload.business_name
        business_address = payload.business_address
        gstin = payload.gstin
    else:
        # Technician: derive invoice type from quotation's tax_mode.
        # NONE  → NON_GST  (admin changed the quotation to Non-GST; technician generates it)
        # B2B   → GST_B2B
        # B2C   → GST_B2C  (default)
        if q_tax_mode == "NONE":
            invoice_type = InvoiceType.NON_GST
        elif q_tax_mode == "B2B":
            invoice_type = InvoiceType.GST_B2B
        else:
            invoice_type = InvoiceType.GST_B2C
        # Pull business/GST details from the quotation itself (captured when
        # the quotation type was set) rather than asking the technician again.
        business_name = payload.business_name or quotation.customer_gst_name
        business_address = payload.business_address or quotation.customer_gst_address
        gstin = payload.gstin or quotation.customer_gst_number

    taxable_amount = round(quotation.subtotal_amount - quotation.discount_amount + quotation.adjustment_amount)
    total_tax = quotation.tax_amount if invoice_type != InvoiceType.NON_GST else 0.0
    cgst_amount = round(total_tax / 2) if invoice_type != InvoiceType.NON_GST else 0
    sgst_amount = round(total_tax / 2) if invoice_type != InvoiceType.NON_GST else 0
    igst_amount = 0.0

    if invoice_type == InvoiceType.GST_B2B and not (gstin and business_name and business_address):
        raise HTTPException(status_code=400, detail="GST B2B invoices require GSTIN, business name, and business address")

    # Resolve domain_id from the booking so the invoice PDF can load the domain profile
    _booking_for_domain = (await db.execute(
        select(Booking).where(Booking.id == quotation.booking_id)
    )).scalar_one_or_none()
    _domain_id_for_invoice = (_booking_for_domain.domain_id
                              if _booking_for_domain and _booking_for_domain.domain_id
                              else None)

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        booking_id=quotation.booking_id,
        domain_id=_domain_id_for_invoice,
        quotation_id=quotation.id,
        generated_by=UUID(current_user["user_id"]),
        invoice_type=invoice_type,
        status=InvoiceStatus.GENERATED,
        business_name=business_name,
        business_address=business_address,
        gstin=gstin,
        taxable_amount=taxable_amount,
        cgst_amount=cgst_amount,
        sgst_amount=sgst_amount,
        igst_amount=igst_amount,
        total_amount=round(taxable_amount + total_tax),
        balance_amount=round(taxable_amount + total_tax),
        notes=payload.notes,
    )
    db.add(invoice)

    quotation.status = QuotationStatus.CONVERTED_TO_INVOICE
    booking = _booking_for_domain  # already fetched above
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

    # Batch-fetch which invoice IDs have a PENDING PAY_LATER transaction
    invoice_ids = [row[0].id for row in rows]
    pay_later_ids: set = set()
    if invoice_ids:
        pay_later_rows = (await db.execute(
            select(PaymentTransaction.invoice_id)
            .where(
                PaymentTransaction.invoice_id.in_(invoice_ids),
                PaymentTransaction.method == PaymentMethod.PAY_LATER,
                PaymentTransaction.status == PaymentStatus.PENDING,
            )
        )).scalars().all()
        pay_later_ids = set(pay_later_rows)

    items = []
    for row in rows:
        inv, bk, cust, tech = row
        cust_name = cust.name if cust else None
        tech_name = None
        if tech and tech.user_id:
            user_row = (await db.execute(select(User).where(User.id == tech.user_id))).scalar_one_or_none()
            tech_name = user_row.name if user_row else None
        items.append(_invoice_summary(inv, bk, cust_name, tech_name, has_pay_later=(inv.id in pay_later_ids)))

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

    # Fetch line items from the linked quotation
    services = []
    parts = []
    services_total = 0.0
    parts_total = 0.0

    if invoice.quotation_id:
        service_rows = (await db.execute(
            select(QuotationServiceItem).where(
                QuotationServiceItem.quotation_id == invoice.quotation_id,
                QuotationServiceItem.is_active == True,
            )
        )).scalars().all()
        for s in service_rows:
            services.append({
                "service_name": s.service_name,
                "quantity": s.quantity or 1,
                "unit_price": int(round(s.unit_price or 0)),
                "total_price": int(round(s.total_price or 0)),
                "appliance_label": s.appliance_label,
            })
            services_total += s.total_price or 0

        part_rows = (await db.execute(
            select(QuotationPartItem).where(
                QuotationPartItem.quotation_id == invoice.quotation_id,
                QuotationPartItem.is_active == True,
            )
        )).scalars().all()
        for p in part_rows:
            parts.append({
                "part_name": p.part_name,
                "part_source": p.part_source.value if p.part_source else "OFFICE_STOCK",
                "quantity": p.quantity or 1,
                "unit_price": int(round(p.unit_price or 0)),
                "total_price": int(round(p.total_price or 0)),
                "vendor_name": p.vendor_name,
                "notes": p.notes,
            })
            parts_total += p.total_price or 0

    data = _invoice_summary(invoice)
    data["services"] = services
    data["parts"] = parts
    data["services_total"] = int(round(services_total))
    data["parts_total"] = int(round(parts_total))
    data["subtotal_amount"] = int(round(invoice.taxable_amount or 0))
    data["tax_amount"] = int(round((invoice.cgst_amount or 0) + (invoice.sgst_amount or 0) + (invoice.igst_amount or 0)))
    data["tax_percent"] = 18  # standard GST; refine if needed

    return success_response(data=data)


def _fetch_logo_reader(url: str):
    """Best-effort remote logo fetch for the PDF header. Returns an ImageReader or None."""
    if not url:
        return None
    try:
        import requests
        resp = requests.get(url, timeout=4)
        if resp.status_code == 200:
            return ImageReader(BytesIO(resp.content))
    except Exception:
        logger.warning("Invoice PDF: could not fetch domain logo from %s", url)
    return None


def _build_invoice_pdf(invoice, booking, customer, domain_profile, services, parts, domain_obj=None, cust_address=None) -> bytes:
    """
    Professional GST Tax Invoice PDF — Bibek Enterprises / Palei Solutions white-label.

    Visual layout (A4, 16 mm margins):
    +---------------------------------------------------------------------------+
    |  [LOGO]  |  Business Name (large)                    |  TAX INVOICE       |
    |          |  Tagline                                  |  INV-XXXXXXXXXXXX  |
    |          |  Address, City, State - PIN               |  Date: DD Mon YYYY |
    |          |  Phone  |  Email                          |  [PAID / PENDING]  |
    |          |  GSTIN  |  PAN                            |                    |
    +---------------------------------------------------------------------------+
    |  BILL TO                       |  BOOKING DETAILS                         |
    |  Customer Name (bold)          |  Booking No  BK-XXXXXXXXX                |
    |  Phone  |  Email               |  Service     Gas Charging                |
    |  Address, City - PIN           |  Scheduled   06 Jul 2026                 |
    |                                |  Invoice Type  GST B2C                   |
    +---------------------------------------------------------------------------+
    |  #  | Description         | Type    | Qty |   Rate (INR) | Amount (INR)   |
    |-----|---------------------|---------|-----|--------------|----------------|
    |  1  | Gas Charging (AC)   | Service |   1 |       850.00 |        850.00  |
    |  2  | Refrigerant Gas R22 | Part    |   1 |       650.00 |        650.00  |
    +---------------------------------------------------------------------------+
    |                                |  Subtotal        INR  1,500.00           |
    |  Amount in Words               |  CGST (9%)       INR    157.50           |
    |  One Thousand Five Hundred...  |  SGST (9%)       INR    157.50           |
    |                                |  --------------------------------         |
    |                                |  TOTAL AMOUNT    INR  2,065.00           |
    |                                |  Balance Due     INR      0.00           |
    +---------------------------------------------------------------------------+
    |  PAYMENT DETAILS                                                           |
    |  Account Name: ...  |  Account No: ...  |  IFSC: ...  |  UPI: ...        |
    +---------------------------------------------------------------------------+
    |  Terms & Conditions (3 lines)                                              |
    +---------------------------------------------------------------------------+
    |  (c) 2026 Bibek Enterprises  |  Computer-generated invoice, no signature  |
    +---------------------------------------------------------------------------+
    """
    # ── Palette ───────────────────────────────────────────────────────────────
    NAVY     = colors.HexColor("#1E3A8A")
    BLUE     = colors.HexColor("#2563EB")
    BLUE_LT  = colors.HexColor("#DBEAFE")
    BLUE_XLT = colors.HexColor("#EFF6FF")
    ORANGE   = colors.HexColor("#EA580C")
    GREEN    = colors.HexColor("#16A34A")
    GREY_DK  = colors.HexColor("#111827")
    GREY_MD  = colors.HexColor("#6B7280")
    GREY_LT  = colors.HexColor("#F9FAFB")
    WHITE    = colors.white
    DIVIDER  = colors.HexColor("#E2E8F0")

    W = 178 * mm   # usable page width

    # ── Style factory ─────────────────────────────────────────────────────────
    base = getSampleStyleSheet()["Normal"]
    def S(name, size=9, color=GREY_DK, bold=False, italic=False,
          align=0, leading=None, sb=0, sa=0):
        fn = ("Helvetica-BoldOblique" if (bold and italic)
              else "Helvetica-Bold" if bold
              else "Helvetica-Oblique" if italic
              else "Helvetica")
        return ParagraphStyle(name, parent=base,
                              fontSize=size, textColor=color, fontName=fn,
                              alignment=align, leading=leading or round(size * 1.4),
                              spaceBefore=sb, spaceAfter=sa)

    # Heading styles
    sH_biz   = S("HBiz",  17, NAVY,    bold=True)
    sH_tag   = S("HTag",   9, GREY_MD, italic=True)
    sH_addr  = S("HAddr",  8, GREY_MD)
    sH_phone = S("HPhone", 8, GREY_MD)
    sH_gst   = S("HGST",   8, GREY_MD)
    # Badge
    sB_title = S("BTit",  11, WHITE,   bold=True,  align=2)
    sB_num   = S("BNum",   8, colors.HexColor("#BFDBFE"), align=2)
    sB_date  = S("BDat",   8, colors.HexColor("#93C5FD"), align=2)
    sB_stat  = S("BStat",  9, WHITE,   bold=True,  align=2)
    # Section headers
    sS_lbl   = S("SLbl",   7, GREY_MD, bold=True)
    sS_val   = S("SVal",   9, GREY_DK, bold=True)
    sS_sub   = S("SSub",   8, GREY_MD)
    # Table
    sTH      = S("TH",     9, WHITE,   bold=True)
    sTD      = S("TD",     9, GREY_DK)
    sTDr     = S("TDr",    9, GREY_DK, align=2)
    sTDbr    = S("TDbr",   9, GREY_DK, bold=True, align=2)
    # Totals
    sTLbl    = S("TLbl",   9, GREY_MD)
    sTVal    = S("TVal",   9, GREY_DK, bold=True, align=2)
    sTGLbl   = S("TGLbl", 10, NAVY,   bold=True)
    sTGVal   = S("TGVal", 10, ORANGE, bold=True, align=2)
    sTBLbl   = S("TBLbl",  9, ORANGE, bold=True)
    sTBVal   = S("TBVal",  9, ORANGE, bold=True, align=2)
    sTDLbl   = S("TDLbl",  9, GREEN)
    sTDVal   = S("TDVal",  9, GREEN,  align=2)
    sWords   = S("Words",  7, GREY_MD, italic=True, align=2)
    # Misc
    sPayH    = S("PayH",   9, NAVY,   bold=True)
    sPayV    = S("PayV",   9, GREY_DK)
    sNote    = S("Note",   8, GREY_MD)
    sFooter  = S("Foot",   8, GREY_MD, align=1)
    sFooter2 = S("Foot2",  7, colors.HexColor("#9CA3AF"), align=1)

    # ── Collect all business info ─────────────────────────────────────────────
    dp = domain_profile   # shorthand
    dom_name = (domain_obj.name if domain_obj else None) if domain_obj else None

    # biz_name is the SERVICE PROVIDER name shown in the invoice header.
    # invoice.business_name holds the *customer's* B2B GST company name (bill-to),
    # NOT the provider — so we must NOT use it here.
    biz_name   = ((dp.business_legal_name if dp else None)
                  or dom_name
                  or "Palei Solutions")
    tagline    = (dp.tagline if dp else None)
    logo_url   = (dp.logo_url if dp else None)
    # Provider's GSTIN for the invoice header — from domain profile only.
    # invoice.gstin is the *customer's* GSTIN (B2B bill-to field), not the provider's.
    gstin      = (dp.gstin if dp else None)
    # Customer's B2B GSTIN (shown in Bill To section if B2B invoice)
    cust_gstin = getattr(invoice, "gstin", None)
    pan        = (dp.pan_number if dp else None)
    phone      = (dp.support_phone if dp else None)
    email      = (dp.support_email if dp else None)

    addr_parts = []
    if dp and dp.office_address: addr_parts.append(dp.office_address)
    city_line = ""
    if dp:
        city_line = ", ".join(filter(None, [dp.office_city, dp.office_state]))
        if dp.office_pincode: city_line += f" - {dp.office_pincode}"
        if dp.office_country and dp.office_country != "India":
            city_line += f", {dp.office_country}"
    if city_line: addr_parts.append(city_line)

    copyright_txt = ((dp.copyright_text if dp else None)
                     or f"(c) {now_ist().year} {biz_name}. All rights reserved.")

    # ── Document setup ────────────────────────────────────────────────────────
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=14*mm, bottomMargin=14*mm,
        leftMargin=16*mm, rightMargin=16*mm,
        title=invoice.invoice_number,
    )
    els = []   # element list

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. HEADER  (logo | business info | invoice badge)
    # ═══════════════════════════════════════════════════════════════════════════
    # Logo
    # Logo — wide landscape format (4:1 ratio, 400×100px as uploaded by admin)
    LOGO_W = 60 * mm
    LOGO_H = 15 * mm
    logo_cell = None
    logo_reader = _fetch_logo_reader(logo_url)
    if logo_reader:
        try:
            from io import BytesIO as _BIO
            import requests as _req
            r = _req.get(logo_url, timeout=5)
            if r.status_code == 200:
                logo_cell = Image(_BIO(r.content), width=LOGO_W, height=LOGO_H)
        except Exception:
            logo_cell = None

    if logo_cell is None:
        # Monogram fallback — navy rectangle with initials
        initials = "".join(w[0].upper() for w in biz_name.split()[:2])
        mono_p = Paragraph(f"<b>{initials}</b>",
                           S("Mono", 16, WHITE, bold=True, align=1))
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
    contact_str = "  |  ".join(filter(None, [phone, email]))
    if contact_str:
        biz_col.append(Paragraph(contact_str, sH_phone))
    gst_str = "  |  ".join(filter(None,
        [f"GSTIN: {gstin}" if gstin else None,
         f"PAN: {pan}" if pan else None]))
    if gst_str:
        biz_col.append(Paragraph(gst_str, sH_gst))

    # Invoice badge column
    inv_date = invoice.created_at.strftime("%d %b %Y") if invoice.created_at else "—"
    status_val = getattr(invoice.status, "value", str(invoice.status)) if invoice.status else "GENERATED"
    is_paid = status_val in ("PAID", "SETTLED", "CLOSED")
    status_label = "PAID" if is_paid else "PAYMENT PENDING"
    status_bg = GREEN if is_paid else ORANGE

    badge_rows = [
        [Paragraph("TAX INVOICE", sB_title)],
        [Paragraph(invoice.invoice_number, sB_num)],
        [Paragraph(f"Date: {inv_date}", sB_date)],
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
    # Status pill below badge
    pill_t = Table([[Paragraph(status_label, S("Pill", 8, WHITE, bold=True, align=1))]],
                   colWidths=[46*mm], rowHeights=[14])
    pill_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), status_bg),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ("ROUNDEDCORNERS",[3]),
    ]))
    badge_col = [badge_t, Spacer(1, 2*mm), pill_t]

    hdr_t = Table([[logo_cell, biz_col, badge_col]],
                  colWidths=[64*mm, 68*mm, 50*mm])
    hdr_t.setStyle(TableStyle([
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("RIGHTPADDING", (0,0),(0,0), 10),
        ("LEFTPADDING",  (2,0),(2,0), 4),
    ]))
    els.append(hdr_t)
    els.append(Spacer(1, 3*mm))

    # Full-width accent rule
    rule_t = Table([[""]], colWidths=[W], rowHeights=[2])
    rule_t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1), BLUE)]))
    els.append(rule_t)
    els.append(Spacer(1, 5*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. BILL TO  /  BOOKING DETAILS
    # ═══════════════════════════════════════════════════════════════════════════
    cust_name  = (customer.name   if customer else "Customer")
    cust_phone = (customer.mobile if customer else None)
    cust_email = getattr(customer, "email", None) if customer else None

    bill_content = [
        Paragraph("BILL TO", sS_lbl),
        Paragraph(cust_name,  sS_val),
    ]
    if cust_phone: bill_content.append(Paragraph(cust_phone, sS_sub))
    if cust_email: bill_content.append(Paragraph(cust_email, sS_sub))
    if booking:
        # Build full address lines using booking fields + CustomerAddress (address_line1/2, city, state, pincode)
        _addr_parts = []
        # Use address_line1 from CustomerAddress if available, else fall back to booking.address_line
        _line1 = (getattr(cust_address, "address_line1", None) if cust_address else None) or booking.address_line
        if _line1: _addr_parts.append(_line1)
        # address_line2 from CustomerAddress
        if cust_address and getattr(cust_address, "address_line2", None):
            _addr_parts.append(cust_address.address_line2)
        # City + State from CustomerAddress if available, else booking.city
        _city  = (getattr(cust_address, "city",  None) if cust_address else None) or booking.city or ""
        _state = (getattr(cust_address, "state", None) if cust_address else None) or ""
        _pin   = (getattr(cust_address, "pincode", None) if cust_address else None) or booking.pincode or ""
        _city_state = ", ".join(filter(None, [_city, _state]))
        if _pin: _city_state += f" - {_pin}"
        if _city_state: _addr_parts.append(_city_state)
        for _aline in _addr_parts:
            bill_content.append(Paragraph(_aline, sS_sub))
    # Show customer's B2B GSTIN if it's a GST B2B invoice
    if cust_gstin:
        bill_content.append(Paragraph(f"GSTIN: {cust_gstin}", sS_sub))
    # Show customer's B2B business name/address if available
    inv_biz_name = getattr(invoice, "business_name", None)
    inv_biz_addr = getattr(invoice, "business_address", None)
    if inv_biz_name:
        bill_content.append(Paragraph(inv_biz_name, sS_sub))
    if inv_biz_addr:
        bill_content.append(Paragraph(inv_biz_addr, sS_sub))

    bk_content = [Paragraph("BOOKING DETAILS", sS_lbl)]
    if booking:
        bk_content.append(Paragraph(booking.booking_number, sS_val))
        if booking.service_name:
            bk_content.append(Paragraph(f"Service: {booking.service_name}", sS_sub))
        if booking.scheduled_date:
            bk_content.append(Paragraph(
                f"Scheduled: {booking.scheduled_date.strftime('%d %b %Y')}", sS_sub))
    inv_type_str = (getattr(invoice.invoice_type, "value", "GST_B2C")
                    .replace("_", " ").title()
                    if getattr(invoice, "invoice_type", None) else "GST Invoice")
    bk_content.append(Paragraph(f"Type: {inv_type_str}", sS_sub))

    info_t = Table([[bill_content, bk_content]], colWidths=[W/2, W/2])
    info_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), BLUE_XLT),
        ("LEFTPADDING",   (0,0),(-1,-1), 10),
        ("RIGHTPADDING",  (0,0),(-1,-1), 10),
        ("TOPPADDING",    (0,0),(-1,-1), 8),
        ("BOTTOMPADDING", (0,0),(-1,-1), 8),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("LINEAFTER",     (0,0),(0,-1),  0.5, BLUE_LT),
        ("LINEBELOW",     (0,-1),(-1,-1),1, BLUE),
        ("ROUNDEDCORNERS",[3]),
    ]))
    els.append(info_t)
    els.append(Spacer(1, 5*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. LINE ITEMS TABLE
    # ═══════════════════════════════════════════════════════════════════════════
    CW = [10*mm, 76*mm, 20*mm, 14*mm, 28*mm, 28*mm]   # #, Desc, Type, Qty, Rate, Amt
    rows = [[
        Paragraph("#",           sTH),
        Paragraph("Description", sTH),
        Paragraph("Type",        sTH),
        Paragraph("Qty",         sTH),
        Paragraph("Rate (INR)",  sTH),
        Paragraph("Amt (INR)",   sTH),
    ]]
    sn = 1
    for s in services:
        desc = s["service_name"]
        if s.get("appliance_label"):
            desc += f" ({s['appliance_label']})"
        rows.append([
            Paragraph(str(sn), sTDr),
            Paragraph(desc, sTD),
            Paragraph("Service", sTD),
            Paragraph(str(s["quantity"]), sTDr),
            Paragraph(f"{s['unit_price']:,.2f}", sTDr),
            Paragraph(f"{s['total_price']:,.2f}", sTDbr),
        ])
        sn += 1
    for p in parts:
        rows.append([
            Paragraph(str(sn), sTDr),
            Paragraph(p["part_name"], sTD),
            Paragraph("Part", sTD),
            Paragraph(str(p["quantity"]), sTDr),
            Paragraph(f"{p['unit_price']:,.2f}", sTDr),
            Paragraph(f"{p['total_price']:,.2f}", sTDbr),
        ])
        sn += 1
    if not services and not parts:
        taxable_fb = float(invoice.taxable_amount or 0)
        rows.append([
            Paragraph("1", sTDr),
            Paragraph("Service Charges", sTD),
            Paragraph("Service", sTD),
            Paragraph("1", sTDr),
            Paragraph(f"{taxable_fb:,.2f}", sTDr),
            Paragraph(f"{taxable_fb:,.2f}", sTDbr),
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

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. TOTALS
    # ═══════════════════════════════════════════════════════════════════════════
    taxable  = float(invoice.taxable_amount or 0)
    cgst     = float(getattr(invoice, "cgst_amount",   0) or 0)
    sgst     = float(getattr(invoice, "sgst_amount",   0) or 0)
    igst     = float(getattr(invoice, "igst_amount",   0) or 0)
    total    = float(invoice.total_amount or 0)
    balance  = float(getattr(invoice, "balance_amount",0) or 0)
    discount = float(getattr(invoice, "discount_amount",0) or 0)
    paid_amt = total - balance
    total_gst= cgst + sgst + igst

    # Determine invoice type for GST/Non-GST display
    _inv_type_raw = str(getattr(getattr(invoice, "invoice_type", None), "value", None) or
                        getattr(invoice, "invoice_type", None) or "GST_B2C")
    _is_non_gst = (_inv_type_raw == "NON_GST")

    tot_rows = []
    tot_rows.append([Paragraph("Subtotal", sTLbl), Paragraph(f"INR {taxable:,.2f}", sTVal)])
    if discount > 0:
        tot_rows.append([Paragraph("Discount", sTDLbl), Paragraph(f"- INR {discount:,.2f}", sTDVal)])
    if not _is_non_gst:
        # Show GST breakdown only for GST invoices
        if cgst > 0:
            tot_rows.append([Paragraph("CGST (9%)", sTLbl), Paragraph(f"INR {cgst:,.2f}", sTVal)])
        if sgst > 0:
            tot_rows.append([Paragraph("SGST (9%)", sTLbl), Paragraph(f"INR {sgst:,.2f}", sTVal)])
        if igst > 0:
            tot_rows.append([Paragraph("IGST (18%)", sTLbl), Paragraph(f"INR {igst:,.2f}", sTVal)])
        elif total_gst > 0 and not (cgst or sgst):
            tot_rows.append([Paragraph("GST", sTLbl), Paragraph(f"INR {total_gst:,.2f}", sTVal)])
    # Grand total
    tot_rows.append([Paragraph("TOTAL", sTGLbl), Paragraph(f"INR {total:,.2f}", sTGVal)])

    if paid_amt > 0 and not is_paid:
        tot_rows.append([Paragraph("Paid",     sTLbl), Paragraph(f"INR {paid_amt:,.2f}", sTVal)])
    if balance > 0:
        tot_rows.append([Paragraph("Balance Due", sTBLbl), Paragraph(f"INR {balance:,.2f}", sTBVal)])

    grand_idx = next(i for i,r in enumerate(tot_rows) if r[0].text == "TOTAL")
    tot_t = Table(tot_rows, colWidths=[42*mm, 38*mm], hAlign="RIGHT")
    tot_style = [
        ("LINEBELOW",     (0,0),(-1, grand_idx-1), 0.4, DIVIDER),
        ("LINEABOVE",     (0, grand_idx),(-1, grand_idx), 1.5, NAVY),
        ("LINEBELOW",     (0, grand_idx),(-1, grand_idx), 1.5, NAVY),
        ("BACKGROUND",    (0, grand_idx),(-1, grand_idx), BLUE_XLT),
        ("TOPPADDING",    (0,0),(-1,-1), 4),
        ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ("LEFTPADDING",   (0,0),(-1,-1), 6),
        ("RIGHTPADDING",  (0,0),(-1,-1), 6),
        ("ROUNDEDCORNERS",[3]),
    ]
    tot_t.setStyle(TableStyle(tot_style))

    # Amount in words
    try:
        from num2words import num2words
        words_str = num2words(int(total), lang="en_IN").title() + " Rupees Only"
    except Exception:
        words_str = ""

    left_cell = []
    if words_str:
        left_cell.append(Paragraph("Amount in Words:", sS_lbl))
        left_cell.append(Paragraph(words_str, sWords))

    outer_t = Table([[left_cell, [tot_t, Spacer(1,2*mm),
                                  Paragraph(words_str, sWords) if words_str else Paragraph("",sNote)]]],
                    colWidths=[W - 86*mm, 86*mm])
    outer_t.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")]))

    # Simpler: just put totals right-aligned with words underneath
    els.append(Table([[Paragraph("",sNote), tot_t]], colWidths=[W-82*mm, 82*mm],
                      style=TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")])))
    if words_str:
        els.append(Table([[Paragraph("",sNote),
                           Paragraph(words_str, sWords)]],
                          colWidths=[W-82*mm, 82*mm]))
    els.append(Spacer(1, 7*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. PAYMENT DETAILS
    # ═══════════════════════════════════════════════════════════════════════════
    # ── Payment section logic ─────────────────────────────────────────────────
    # If fully paid → show transaction summary (what was paid, how, when).
    # If unpaid / partial → show our bank/UPI details so customer can pay.
    def _pay_table(header, rows_data):
        pay_rows = [[Paragraph(header, sPayH), Paragraph("", sNote)]]
        for lbl, val in rows_data:
            pay_rows.append([Paragraph(lbl, sTLbl), Paragraph(str(val), sPayV)])
        pt = Table(pay_rows, colWidths=[44*mm, W-44*mm])
        pt.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE_XLT),
            ("SPAN",          (0,0),(-1,0)),
            ("LINEBELOW",     (0,0),(-1,0),  0.8, BLUE_LT),
            ("LINEBELOW",     (0,1),(-1,-1), 0.3, DIVIDER),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 8),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("ROUNDEDCORNERS",[3]),
        ]))
        return pt

    if is_paid:
        # Show transaction details (paid_at, amount, method via paid_transactions)
        txn_data = []
        if invoice.paid_at:
            try:
                _paid_dt = invoice.paid_at.strftime("%d %b %Y, %I:%M %p")
            except Exception:
                _paid_dt = str(invoice.paid_at)[:16]
            txn_data.append(("Paid On", _paid_dt))
        txn_data.append(("Amount Paid", f"INR {total:,.2f}"))
        if getattr(invoice, "paid_transactions", None):
            for t in invoice.paid_transactions:
                if t.get("transaction_number"):
                    txn_data.append(("Transaction No.", t["transaction_number"]))
                if t.get("method"):
                    txn_data.append(("Payment Method", t["method"].replace("_", " ").title()))
                if t.get("provider_payment_id"):
                    txn_data.append(("Payment ID", t["provider_payment_id"]))
        if txn_data:
            els.append(_pay_table("PAYMENT RECEIVED", txn_data))
            els.append(Spacer(1, 6*mm))
    elif dp and (dp.bank_account_number or dp.upi_id):
        # Show our bank/UPI details so customer knows where to pay
        pay_data = []
        if dp.bank_account_name:   pay_data.append(("Account Name", dp.bank_account_name))
        if dp.bank_account_number: pay_data.append(("Account No.",  dp.bank_account_number))
        if dp.bank_ifsc:           pay_data.append(("IFSC Code",    dp.bank_ifsc))
        if dp.bank_name:
            bank = dp.bank_name + (f", {dp.bank_branch}" if dp.bank_branch else "")
            pay_data.append(("Bank", bank))
        if dp.upi_id:              pay_data.append(("UPI ID",       dp.upi_id))
        if balance > 0:
            pay_data.insert(0, ("Amount Due", f"INR {balance:,.2f}"))
        if pay_data:
            els.append(_pay_table("PAY TO (BANK / UPI)", pay_data))
            els.append(Spacer(1, 6*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. NOTES + TERMS
    # ═══════════════════════════════════════════════════════════════════════════
    notes_text = getattr(invoice, "notes", None)
    terms_block = []
    if notes_text:
        terms_block.append(Paragraph(f"Notes: {notes_text}", sNote))
        terms_block.append(Spacer(1, 2*mm))
    terms_block.append(Paragraph("Terms & Conditions", sPayH))
    for t in [
        "1. This invoice is system-generated and valid without a physical signature.",
        "2. Goods/services once sold will not be taken back or exchanged.",
        "3. For disputes or queries, contact us within 7 days of the invoice date.",
    ]:
        terms_block.append(Paragraph(t, sNote))

    els.append(KeepTogether(terms_block))
    els.append(Spacer(1, 7*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. FOOTER
    # ═══════════════════════════════════════════════════════════════════════════
    foot_rule = Table([[""]], colWidths=[W], rowHeights=[0.8])
    foot_rule.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1), BLUE_LT)]))
    els.append(foot_rule)
    els.append(Spacer(1, 3*mm))
    els.append(Paragraph(copyright_txt, sFooter))
    els.append(Paragraph(
        "This is a computer-generated invoice and does not require a physical signature.",
        sFooter2,
    ))

    doc.build(els)
    buf.seek(0)
    return buf.read()


@router.get("/{invoice_id}/pdf", summary="Download PDF")
async def get_invoice_pdf(
    invoice_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id)
    await _ensure_access(db, invoice, current_user)

    booking = (await db.execute(select(Booking).where(Booking.id == invoice.booking_id))).scalar_one_or_none()

    customer = None
    if booking:
        customer = (await db.execute(select(Customer).where(Customer.id == booking.customer_id))).scalar_one_or_none()

    domain_profile = None
    domain_obj = None
    domain_id = invoice.domain_id or (booking.domain_id if booking else None)

    # If domain_id is not on the invoice (legacy invoices created before the fix),
    # fall back to looking up the domain by slug from the domain table.
    if not domain_id:
        # Try to resolve via the booking's domain_id one more time; if still missing,
        # load the first/only active domain as a last resort (single-tenant deployment).
        fallback_domain = (await db.execute(
            select(Domain).order_by(Domain.sort_order.asc()).limit(1)
        )).scalar_one_or_none()
        if fallback_domain:
            domain_id = fallback_domain.id

    if domain_id:
        domain_profile = (await db.execute(
            select(DomainProfile).where(DomainProfile.domain_id == domain_id)
        )).scalar_one_or_none()
        domain_obj = (await db.execute(
            select(Domain).where(Domain.id == domain_id)
        )).scalar_one_or_none()

    services, parts = [], []
    if invoice.quotation_id:
        service_rows = (await db.execute(
            select(QuotationServiceItem).where(
                QuotationServiceItem.quotation_id == invoice.quotation_id,
                QuotationServiceItem.is_active == True,
            )
        )).scalars().all()
        services = [{
            "service_name": s.service_name, "quantity": s.quantity or 1,
            "unit_price": int(round(s.unit_price or 0)), "total_price": int(round(s.total_price or 0)),
            "appliance_label": s.appliance_label,
        } for s in service_rows]

        part_rows = (await db.execute(
            select(QuotationPartItem).where(
                QuotationPartItem.quotation_id == invoice.quotation_id,
                QuotationPartItem.is_active == True,
            )
        )).scalars().all()
        parts = [{
            "part_name": p.part_name, "quantity": p.quantity or 1,
            "unit_price": int(round(p.unit_price or 0)), "total_price": int(round(p.total_price or 0)),
        } for p in part_rows]

    # Load CustomerAddress for full address in PDF (address_line1, address_line2, city, state, pincode)
    cust_address = None
    try:
        from app.models.customer import CustomerAddress as _CustomerAddress
        if booking and getattr(booking, "address_id", None):
            cust_address = (await db.execute(
                select(_CustomerAddress).where(_CustomerAddress.id == booking.address_id)
            )).scalar_one_or_none()
    except Exception:
        cust_address = None

    try:
        pdf_bytes = _build_invoice_pdf(invoice, booking, customer, domain_profile, services, parts, domain_obj, cust_address)
    except Exception:
        logger.exception("Premium invoice PDF generation failed, falling back to plain layout")
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setTitle(invoice.invoice_number)
        pdf.drawString(50, 800, f"Invoice: {invoice.invoice_number}")
        pdf.drawString(50, 780, f"Total Amount: INR {invoice.total_amount:.2f}")
        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        pdf_bytes = buffer.read()

    return StreamingResponse(
        BytesIO(pdf_bytes),
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
    invoice.sent_email_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE
    await db.commit()
    return success_response(
        data={"invoice_id": str(invoice.id), "recipient": payload.recipient, "sent_at": iso(invoice.sent_email_at)},
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
    invoice.sent_whatsapp_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE
    await db.commit()
    return success_response(
        data={"invoice_id": str(invoice.id), "recipient": payload.recipient, "sent_at": iso(invoice.sent_whatsapp_at)},
        message="Invoice WhatsApp queued successfully",
    )
