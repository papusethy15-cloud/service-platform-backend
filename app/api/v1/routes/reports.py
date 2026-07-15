from app.utils.timezone import ist_midnight_utc, ist_end_of_day_utc
from datetime import date
import sqlalchemy as sa

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyStaff
from app.core.database import get_db
from app.services.reporting import (
    build_customer_report,
    build_gst_report,
    build_placeholder_report,
    build_revenue_report,
)
from app.utils.response import success_response

router = APIRouter()


def _handle_report_range_error(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/revenue", summary="Revenue report")
async def revenue_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    period: str | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    # Convert year/month/period to date range if explicit dates not given
    if not start_date and not end_date and year and month:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)
    try:
        report = await build_revenue_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/gst", summary="GST report")
async def gst_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    # Convert year/month to date range if explicit dates not given
    if not start_date and not end_date and year and month:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)
    try:
        report = await build_gst_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/commission", summary="Commission report")
async def commission_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("commission", "Commission source tables are not implemented yet"),
        message="Commission report is waiting on the commission module",
    )


@router.get("/inventory", summary="Inventory report")
async def inventory_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("inventory", "Inventory source tables are not implemented yet"),
        message="Inventory report is waiting on the inventory module",
    )


@router.get("/amc", summary="AMC report")
async def amc_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("amc", "AMC source tables are not implemented yet"),
        message="AMC report is waiting on the AMC module",
    )


@router.get("/warranty", summary="Warranty report")
async def warranty_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("warranty", "Warranty source tables are not implemented yet"),
        message="Warranty report is waiting on the warranty module",
    )


@router.get("/customer", summary="Customer report")
async def customer_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_customer_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/franchise", summary="Franchise report")
async def franchise_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("franchise", "Franchise source tables are not implemented yet"),
        message="Franchise report is waiting on the franchise module",
    )


@router.get("/technician", summary="Technician performance report")
async def technician_report(
    technician_id: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    period: str = Query("monthly", regex="^(daily|weekly|monthly|yearly)$"),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    """Returns booking counts, revenue, ratings, attendance for one or all technicians."""
    from sqlalchemy import select, func, and_
    from app.models.technician import Technician
    from app.models.booking import Booking
    from app.models.payment import PaymentTransaction, PaymentStatus
    from app.models.attendance import AttendanceRecord
    from uuid import UUID

    # Date range defaults
    from datetime import datetime, timezone
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        from dateutil.relativedelta import relativedelta
        start_date = end_date - relativedelta(months=1)

    start_dt = ist_midnight_utc(start_date)
    end_dt = ist_end_of_day_utc(end_date)

    # Base technician query
    tech_q = select(Technician).where(Technician.is_active == True)
    if technician_id:
        try:
            tech_q = tech_q.where(Technician.id == UUID(technician_id))
        except Exception:
            pass
    technicians = (await db.execute(tech_q)).scalars().all()

    results = []
    for tech in technicians:
        # Booking stats
        booking_q = select(
            func.count(Booking.id).label("total"),
            func.sum(
                func.cast(Booking.status == "COMPLETED", sa.Integer)
            ).label("completed"),
        ).where(
            Booking.technician_id == tech.id,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        )
        bk_row = (await db.execute(booking_q)).one()
        total_bookings = bk_row.total or 0
        completed = int(bk_row.completed or 0)

        # Revenue
        rev_q = select(func.sum(PaymentTransaction.amount)).join(
            Booking, Booking.id == PaymentTransaction.booking_id
        ).where(
            Booking.technician_id == tech.id,
            PaymentTransaction.status == PaymentStatus.SUCCESS,
            PaymentTransaction.paid_at >= start_dt,
            PaymentTransaction.paid_at <= end_dt,
        )
        revenue = (await db.execute(rev_q)).scalar_one() or 0.0

        results.append({
            "technician_id": str(tech.id),
            "technician_name": tech.name,
            "mobile": tech.mobile,
            "total_bookings": total_bookings,
            "completed_bookings": completed,
            "completion_rate": round((completed / total_bookings * 100) if total_bookings else 0, 1),
            "revenue_generated": int(round(revenue)),
            "period": {"start": str(start_date), "end": str(end_date)},
        })

    results.sort(key=lambda x: x["revenue_generated"], reverse=True)
    return success_response(data={
        "technicians": results,
        "period": {"start": str(start_date), "end": str(end_date)},
        "total_technicians": len(results),
    })


# ── GET /reports/technician-detail ──────────────────────────────────────────
@router.get("/technician-detail", summary="Full technician report [Admin]")
async def technician_detail_report(
    technician_id: str = Query(..., description="Technician UUID"),
    period: str = Query("monthly", regex="^(weekly|monthly|yearly)$"),
    year: int = Query(None),
    month: int = Query(None),
    week: int = Query(None),          # ISO week number (1-53); used when period=weekly
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a full, detailed report for a single technician covering the
    requested time window:

    • Summary KPIs — total / completed / cancelled bookings, revenue,
      cash vs online split, avg rating, completion rate
    • Booking list with status, quotation number, invoice number, amount
    • Payment breakdown (CASH vs ONLINE/RAZORPAY totals)
    • Quotation list with status
    • Rating list from customers
    """
    import calendar
    from datetime import datetime, timedelta
    from uuid import UUID
    from sqlalchemy import select, func, case, and_, or_
    from app.models.technician import Technician, TechnicianRating
    from app.models.booking import Booking, BookingStatus
    from app.models.quotation import Quotation
    from app.models.invoice import Invoice
    from app.models.payment import PaymentTransaction, PaymentStatus, PaymentMethod
    from app.models.commission import Commission
    from app.models.wallet import Wallet, WalletTransaction, WithdrawalRequest

    # ── Resolve date range ─────────────────────────────────────────────────
    today = date.today()

    if start_date and end_date:
        sd, ed = start_date, end_date
    elif period == "weekly":
        y = year or today.isocalendar()[0]
        w = week or today.isocalendar()[1]
        # ISO week: Monday = day 1
        jan4 = date(y, 1, 4)
        week_start = jan4 + timedelta(weeks=w - 1, days=-jan4.weekday())
        sd = week_start
        ed = week_start + timedelta(days=6)
    elif period == "monthly":
        y = year or today.year
        m = month or today.month
        sd = date(y, m, 1)
        ed = date(y, m, calendar.monthrange(y, m)[1])
    else:  # yearly
        y = year or today.year
        sd = date(y, 1, 1)
        ed = date(y, 12, 31)

    start_dt = ist_midnight_utc(sd)
    end_dt   = ist_end_of_day_utc(ed)

    # ── Fetch technician ───────────────────────────────────────────────────
    try:
        tid = UUID(technician_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid technician_id")

    tech = (await db.execute(
        select(Technician).where(Technician.id == tid)
    )).scalar_one_or_none()
    if not tech:
        raise HTTPException(status_code=404, detail="Technician not found")

    # ── Bookings in range ──────────────────────────────────────────────────
    bookings = (await db.execute(
        select(Booking).where(
            Booking.technician_id == tid,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        ).order_by(Booking.created_at.desc())
    )).scalars().all()

    booking_ids = [b.id for b in bookings]

    # ── Quotations for those bookings ──────────────────────────────────────
    quotations = []
    if booking_ids:
        quotations = (await db.execute(
            select(Quotation).where(Quotation.booking_id.in_(booking_ids))
        )).scalars().all()

    quot_by_booking: dict = {}
    for q in quotations:
        quot_by_booking.setdefault(q.booking_id, []).append(q)

    # ── Invoices for those bookings ────────────────────────────────────────
    invoices = []
    if booking_ids:
        invoices = (await db.execute(
            select(Invoice).where(Invoice.booking_id.in_(booking_ids))
        )).scalars().all()

    inv_by_booking: dict = {}
    for inv in invoices:
        inv_by_booking[inv.booking_id] = inv

    # ── Payments for those bookings ────────────────────────────────────────
    payments = []
    if booking_ids:
        payments = (await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.booking_id.in_(booking_ids),
                PaymentTransaction.status == PaymentStatus.SUCCESS,
            )
        )).scalars().all()

    pay_by_booking: dict = {}
    for p in payments:
        pay_by_booking.setdefault(p.booking_id, []).append(p)

    # ── Ratings given to this technician ──────────────────────────────────
    ratings_rows = (await db.execute(
        select(TechnicianRating).where(
            TechnicianRating.technician_id == tid,
            TechnicianRating.created_at >= start_dt,
            TechnicianRating.created_at <= end_dt,
        ).order_by(TechnicianRating.created_at.desc())
    )).scalars().all()

    # ── Wallet for this technician ────────────────────────────────────────────
    wallet_row = (await db.execute(
        select(Wallet).where(Wallet.technician_id == tid)
    )).scalar_one_or_none()

    wallet_txns = []
    withdrawal_rows_raw = []
    if wallet_row:
        wallet_txns = (await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == wallet_row.id,
                WalletTransaction.created_at >= start_dt,
                WalletTransaction.created_at <= end_dt,
            ).order_by(WalletTransaction.created_at.desc())
        )).scalars().all()

        withdrawal_rows_raw = (await db.execute(
            select(WithdrawalRequest).where(
                WithdrawalRequest.technician_id == tid,
                WithdrawalRequest.created_at >= start_dt,
                WithdrawalRequest.created_at <= end_dt,
            ).order_by(WithdrawalRequest.created_at.desc())
        )).scalars().all()

    # ── Commissions for this technician in range ────────────────────────────
    commission_rows = (await db.execute(
        select(Commission).where(
            Commission.technician_id == tid,
            Commission.created_at >= start_dt,
            Commission.created_at <= end_dt,
        ).order_by(Commission.created_at.desc())
    )).scalars().all()

    # ── Aggregate KPIs ─────────────────────────────────────────────────────
    total_bookings     = len(bookings)
    completed_bookings = sum(1 for b in bookings if b.status in (
        BookingStatus.COMPLETED, BookingStatus.PAID,
        BookingStatus.CLOSED, BookingStatus.SETTLED,
        BookingStatus.PAYMENT_PENDING, BookingStatus.INVOICE_GENERATED,
    ))
    cancelled_bookings = sum(1 for b in bookings if b.status == BookingStatus.CANCELLED)
    active_bookings    = total_bookings - completed_bookings - cancelled_bookings

    total_cash   = sum(p.amount for p in payments if p.method == PaymentMethod.CASH)
    total_online = sum(p.amount for p in payments if p.method == PaymentMethod.RAZORPAY)
    total_revenue = total_cash + total_online

    avg_rating = (
        round(sum(r.rating for r in ratings_rows) / len(ratings_rows), 2)
        if ratings_rows else None
    )

    # Commission aggregates
    total_commission_earned  = sum(c.commission_amount or 0 for c in commission_rows)
    total_commission_pending = sum(c.commission_amount or 0 for c in commission_rows if c.status == "PENDING")
    total_commission_paid    = sum(c.commission_amount or 0 for c in commission_rows if c.status == "PAID")
    total_commission_approved = sum(c.commission_amount or 0 for c in commission_rows if c.status == "APPROVED")

    # ── Build booking rows ─────────────────────────────────────────────────
    def _iso(dt):
        return dt.isoformat() if dt else None

    booking_rows = []
    for b in bookings:
        q_list  = quot_by_booking.get(b.id, [])
        inv     = inv_by_booking.get(b.id)
        p_list  = pay_by_booking.get(b.id, [])

        # Pick the most advanced quotation
        quot = None
        if q_list:
            _order = ['APPROVED', 'SUBMITTED', 'DRAFT', 'REJECTED']
            for s in _order:
                found = next((q for q in q_list if q.status.value == s), None)
                if found:
                    quot = found
                    break
            if quot is None:
                quot = q_list[0]

        paid_cash   = sum(p.amount for p in p_list if p.method == PaymentMethod.CASH)
        paid_online = sum(p.amount for p in p_list if p.method == PaymentMethod.RAZORPAY)

        booking_rows.append({
            "booking_id":       str(b.id),
            "booking_number":   b.booking_number,
            "service_name":     b.service_name if hasattr(b, 'service_name') else None,
            "status":           b.status.value,
            "scheduled_date":   str(b.scheduled_date) if b.scheduled_date else None,
            "total_amount":     b.total_amount or 0.0,
            "cancelled_reason": b.cancelled_reason,
            "created_at":       _iso(b.created_at),
            "quotation_number": quot.quotation_number if quot else None,
            "quotation_status": quot.status.value if quot else None,
            "invoice_number":   inv.invoice_number if inv else None,
            "invoice_total":    inv.total_amount if inv else None,
            "invoice_status":   inv.status.value if inv else None,
            "paid_cash":        paid_cash,
            "paid_online":      paid_online,
            "paid_total":       paid_cash + paid_online,
        })

    # ── Build rating rows ──────────────────────────────────────────────────
    rating_rows = [{
        "rating":     r.rating,
        "review":     r.review if hasattr(r, 'review') else None,
        "booking_id": str(r.booking_id) if r.booking_id else None,
        "created_at": _iso(r.created_at),
    } for r in ratings_rows]

    # ── Build quotation summary rows ───────────────────────────────────────
    quot_rows = [{
        "quotation_number": q.quotation_number,
        "booking_id":       str(q.booking_id),
        "status":           q.status.value,
        "total_amount":     q.total_amount if hasattr(q, 'total_amount') else None,
        "created_at":       _iso(q.created_at),
    } for q in quotations]

    # ── Build wallet transaction rows ──────────────────────────────────────
    wallet_txn_rows = [{
        "id":              str(t.id),
        "type":            t.transaction_type,
        "amount":          int(round(t.amount or 0)),
        "balance_before":  int(round(t.balance_before or 0)),
        "balance_after":   int(round(t.balance_after or 0)),
        "description":     t.description,
        "reference_id":    t.reference_id,
        "status":          t.status,
        "created_at":      _iso(t.created_at),
    } for t in wallet_txns]

    withdrawal_detail_rows = [{
        "id":                str(wr.id),
        "amount":            int(round(wr.amount or 0)),
        "status":            wr.status,
        "upi_id":            wr.upi_id,
        "bank_account":      wr.bank_account,
        "bank_ifsc":         wr.bank_ifsc,
        "bank_name":         wr.bank_name,
        "notes":             wr.notes,
        "admin_notes":       wr.admin_notes,
        "payment_reference": wr.payment_reference,
        "reviewed_at":       _iso(wr.reviewed_at),
        "created_at":        _iso(wr.created_at),
    } for wr in withdrawal_rows_raw]

    # Wallet summary
    wallet_credits  = sum(t.amount for t in wallet_txns if t.transaction_type == "CREDIT")
    wallet_debits   = sum(t.amount for t in wallet_txns if t.transaction_type == "DEBIT")
    wallet_withdrawals = sum(t.amount for t in wallet_txns if t.transaction_type == "WITHDRAWAL")

    # ── Build commission rows ───────────────────────────────────────────────
    commission_detail_rows = [{
        "id":                str(c.id),
        "booking_id":        str(c.booking_id) if c.booking_id else None,
        "item_type":         c.item_type,
        "item_name":         c.item_name,
        "base_amount":       c.base_amount,
        "commission_amount": c.commission_amount,
        "status":            c.status,
        "payout_date":       _iso(c.payout_date),
        "part_source":       c.part_source,
        "notes":             c.notes,
        "created_at":        _iso(c.created_at),
    } for c in commission_rows]

    return success_response(data={
        "technician": {
            "id":      str(tech.id),
            "name":    tech.name,
            "mobile":  tech.mobile,
            "email":   tech.email,
            "city":    tech.city,
            "rating":  tech.rating,
            "profile_image": tech.profile_image,
        },
        "period": {
            "type":       period,
            "start_date": str(sd),
            "end_date":   str(ed),
        },
        "summary": {
            "total_bookings":     total_bookings,
            "completed_bookings": completed_bookings,
            "cancelled_bookings": cancelled_bookings,
            "active_bookings":    active_bookings,
            "completion_rate":    round((completed_bookings / total_bookings * 100) if total_bookings else 0, 1),
            "total_revenue":      int(round(total_revenue)),
            "total_cash":         int(round(total_cash)),
            "total_online":       int(round(total_online)),
            "avg_rating":         avg_rating,
            "total_ratings":      len(ratings_rows),
            "total_quotations":   len(quotations),
            "total_invoices":     len(invoices),
        },
        "bookings":     booking_rows,
        "quotations":   quot_rows,
        "ratings":      rating_rows,
        "wallet": {
            "balance":         int(round(wallet_row.balance or 0)) if wallet_row else 0,
            "total_earned":    int(round(wallet_row.total_earned or 0)) if wallet_row else 0,
            "total_withdrawn": int(round(wallet_row.total_withdrawn or 0)) if wallet_row else 0,
        } if wallet_row else None,
        "wallet_summary": {
            "credits_in_period":      int(round(wallet_credits)),
            "debits_in_period":       int(round(wallet_debits)),
            "withdrawals_in_period":  int(round(wallet_withdrawals)),
            "txn_count":              len(wallet_txns),
            "withdrawal_count":       len(withdrawal_rows_raw),
        },
        "wallet_transactions": wallet_txn_rows,
        "withdrawal_requests":  withdrawal_detail_rows,
        "commissions":  commission_detail_rows,
        "commission_summary": {
            "total_earned":   int(round(total_commission_earned)),
            "total_pending":  int(round(total_commission_pending)),
            "total_approved": int(round(total_commission_approved)),
            "total_paid":     int(round(total_commission_paid)),
            "total_records":  len(commission_rows),
        },
    })


# ══════════════════════════════════════════════════════════════════════════════
# PDF GENERATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_technician_report_pdf(report_data: dict) -> bytes:
    """
    Generate a professional A4 PDF for the technician detail report.
    Matches the visual style of the invoice PDF.
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )
    from app.utils.timezone import now_ist

    # ── Palette (same as invoice) ─────────────────────────────────────────────
    NAVY    = colors.HexColor("#1E3A8A")
    BLUE    = colors.HexColor("#2563EB")
    BLUE_LT = colors.HexColor("#DBEAFE")
    ORANGE  = colors.HexColor("#EA580C")
    GREEN   = colors.HexColor("#16A34A")
    RED     = colors.HexColor("#DC2626")
    AMBER   = colors.HexColor("#D97706")
    TEAL    = colors.HexColor("#0891B2")
    PURPLE  = colors.HexColor("#7C3AED")
    GREY_DK = colors.HexColor("#111827")
    GREY_MD = colors.HexColor("#6B7280")
    GREY_LT = colors.HexColor("#F9FAFB")
    WHITE   = colors.white
    DIVIDER = colors.HexColor("#E2E8F0")

    W = 178 * mm

    base = getSampleStyleSheet()["Normal"]
    def S(name, size=9, color=GREY_DK, bold=False, italic=False, align=0, leading=None, sb=0, sa=0):
        fn = ("Helvetica-BoldOblique" if (bold and italic)
              else "Helvetica-Bold" if bold
              else "Helvetica-Oblique" if italic
              else "Helvetica")
        return ParagraphStyle(name, parent=base,
                              fontSize=size, textColor=color, fontName=fn,
                              alignment=align, leading=leading or round(size * 1.4),
                              spaceBefore=sb, spaceAfter=sa)

    sTitle    = S("Title",   18, NAVY,   bold=True)
    sSub      = S("Sub",      9, GREY_MD, italic=True)
    sSecHdr   = S("SecHdr",  10, WHITE,  bold=True)
    sLabel    = S("Label",    8, GREY_MD, bold=True)
    sVal      = S("Val",      9, GREY_DK, bold=True)
    sSub2     = S("Sub2",     8, GREY_MD)
    sTH       = S("TH",       8, WHITE,  bold=True)
    sTHr      = S("THr",      8, WHITE,  bold=True, align=2)
    sTD       = S("TD",       8, GREY_DK)
    sTDr      = S("TDr",      8, GREY_DK, align=2)
    sTDbold   = S("TDbold",   8, GREY_DK, bold=True)
    sTDgreen  = S("TDgreen",  8, GREEN,  bold=True, align=2)
    sTDred    = S("TDred",    8, RED,    bold=True, align=2)
    sTDamber  = S("TDamber",  8, AMBER,  bold=True, align=2)
    sTDteal   = S("TDteal",   8, TEAL,   bold=True, align=2)
    sTDpurple = S("TDpurple", 8, PURPLE, bold=True, align=2)
    sFooter   = S("Footer",   7, GREY_MD, align=1)

    INR = lambda v: f"\u20b9{(v or 0):,.2f}"
    iso_d = lambda s: s[:10] if s else "—"

    tech   = report_data.get("technician", {})
    period = report_data.get("period", {})
    s      = report_data.get("summary", {})
    cs     = report_data.get("commission_summary", {})
    ws     = report_data.get("wallet_summary", {})
    wallet = report_data.get("wallet", {})

    bookings   = report_data.get("bookings", [])
    quotations = report_data.get("quotations", [])
    payments   = [b for b in bookings if (b.get("paid_total") or 0) > 0]
    commissions = report_data.get("commissions", [])
    wallet_txns = report_data.get("wallet_transactions", [])
    withdrawals = report_data.get("withdrawal_requests", [])
    ratings     = report_data.get("ratings", [])

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=14*mm, bottomMargin=14*mm,
                            leftMargin=16*mm, rightMargin=16*mm,
                            title=f"Technician Report — {tech.get('name','')}")
    els = []

    def section_header(title):
        els.append(Spacer(1, 6*mm))
        els.append(Table(
            [[Paragraph(title, sSecHdr)]],
            colWidths=[W],
            style=TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), NAVY),
                ("ROWPADDING", (0,0), (-1,-1), 5),
                ("ROUNDEDCORNERS", [4]),
            ])
        ))
        els.append(Spacer(1, 3*mm))

    def kpi_row(items):
        """items: list of (label, value, color)"""
        n = len(items)
        w = W / n
        header_row = [[Paragraph(lbl, S(f"kl{i}", 7, GREY_MD, bold=True))] for i,(lbl,_,_) in enumerate(items)]
        value_row  = [[Paragraph(str(val), S(f"kv{i}", 12, col, bold=True))] for i,(_,val,col) in enumerate(items)]
        combined   = [[h[0], v[0]] for h,v in zip(header_row, value_row)]
        # Use one row per card in a grid table
        cells = [[
            Table([[Paragraph(lbl, S(f"l{i}", 7, GREY_MD, bold=True))],
                   [Paragraph(str(val), S(f"v{i}", 13, col, bold=True))]],
                  style=TableStyle([
                      ("BACKGROUND",  (0,0),(-1,-1), GREY_LT),
                      ("ROWPADDING",  (0,0),(-1,-1), 4),
                      ("LEFTPADDING", (0,0),(-1,-1), 8),
                      ("BOX", (0,0),(-1,-1), 0.5, DIVIDER),
                  ]))
            for i,(lbl,val,col) in enumerate(items)
        ]]
        els.append(Table(cells, colWidths=[w]*n,
                         style=TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),
                                           ("RIGHTPADDING",(0,0),(-1,-1),2)])))

    # ── HEADER ────────────────────────────────────────────────────────────────
    now_str = now_ist().strftime("%d %b %Y, %I:%M %p IST")
    header_data = [[
        Table([
            [Paragraph("TECHNICIAN REPORT", S("RptTitle", 16, NAVY, bold=True))],
            [Paragraph(f"Generated: {now_str}", S("RptSub", 8, GREY_MD, italic=True))],
        ], style=TableStyle([("LEFTPADDING",(0,0),(-1,-1),0)])),
        Table([
            [Paragraph(tech.get("name","—"), S("TName", 14, NAVY, bold=True, align=2))],
            [Paragraph(f"{tech.get('mobile','—')}  •  {tech.get('city','—') or '—'}", S("TMob", 8, GREY_MD, align=2))],
            [Paragraph(f"Period: {period.get('type','').upper()}  |  {iso_d(period.get('start_date'))} → {iso_d(period.get('end_date'))}", S("TPer", 8, BLUE, italic=True, align=2))],
        ], style=TableStyle([("LEFTPADDING",(0,0),(-1,-1),0)])),
    ]]
    els.append(Table(header_data, colWidths=[W*0.5, W*0.5],
                     style=TableStyle([
                         ("LINEBELOW", (0,0),(-1,0), 1.5, NAVY),
                         ("BOTTOMPADDING", (0,0),(-1,-1), 8),
                     ])))
    els.append(Spacer(1, 4*mm))

    # ── BOOKING SUMMARY KPIs ──────────────────────────────────────────────────
    section_header("📋  BOOKING SUMMARY")
    kpi_row([
        ("Total Bookings",  s.get("total_bookings",0),      NAVY),
        ("Completed",       s.get("completed_bookings",0),  GREEN),
        ("Cancelled",       s.get("cancelled_bookings",0),  RED),
        ("Completion Rate", f"{s.get('completion_rate',0)}%", BLUE),
    ])
    els.append(Spacer(1, 2*mm))
    kpi_row([
        ("Total Revenue",    INR(s.get("total_revenue",0)),   GREEN),
        ("Cash Collected",   INR(s.get("total_cash",0)),      AMBER),
        ("Online Collected", INR(s.get("total_online",0)),    TEAL),
        ("Avg Rating",       f"{s.get('avg_rating','N/A')} ★" if s.get("avg_rating") else "N/A",  ORANGE),
    ])

    # ── COMMISSION SUMMARY ─────────────────────────────────────────────────────
    if cs:
        section_header("🏆  COMMISSION SUMMARY")
        kpi_row([
            ("Earned",   INR(cs.get("total_earned",0)),   PURPLE),
            ("Pending",  INR(cs.get("total_pending",0)),  AMBER),
            ("Approved", INR(cs.get("total_approved",0)), TEAL),
            ("Paid",     INR(cs.get("total_paid",0)),     GREEN),
        ])

    # ── WALLET SUMMARY ────────────────────────────────────────────────────────
    if wallet:
        section_header("💰  WALLET SUMMARY")
        kpi_row([
            ("Current Balance",    INR(wallet.get("balance",0)),          NAVY),
            ("Total Earned",       INR(wallet.get("total_earned",0)),     GREEN),
            ("Total Withdrawn",    INR(wallet.get("total_withdrawn",0)),  AMBER),
            ("Period Credits",     INR(ws.get("credits_in_period",0)),    TEAL),
        ])
        els.append(Spacer(1, 2*mm))
        kpi_row([
            ("Period Debits",      INR(ws.get("debits_in_period",0)),     RED),
            ("Period Withdrawals", INR(ws.get("withdrawals_in_period",0)), PURPLE),
            ("Transactions",       ws.get("txn_count",0),                 GREY_DK),
            ("Withdrawal Reqs",    ws.get("withdrawal_count",0),          ORANGE),
        ])

    # ── BOOKINGS TABLE ────────────────────────────────────────────────────────
    section_header(f"📋  BOOKINGS  ({len(bookings)})")
    if bookings:
        tdata = [[
            Paragraph("#", sTH), Paragraph("Booking No.", sTH), Paragraph("Service", sTH),
            Paragraph("Status", sTH), Paragraph("Date", sTH), Paragraph("Amount", sTHr),
            Paragraph("Cash", sTHr), Paragraph("Online", sTHr),
        ]]
        for i, b in enumerate(bookings):
            amt    = b.get("total_amount") or 0
            cash   = b.get("paid_cash") or 0
            online = b.get("paid_online") or 0
            status = (b.get("status") or "").replace("_"," ")
            sc = GREEN if "COMPLET" in status or "PAID" in status or "SETTLED" in status else RED if "CANCEL" in status else AMBER
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph(b.get("booking_number","—"), sTDbold),
                Paragraph((b.get("service_name") or "—")[:28], sTD),
                Paragraph(status, S(f"bs{i}", 8, sc, bold=True)),
                Paragraph(iso_d(b.get("scheduled_date") or b.get("created_at")), sTD),
                Paragraph(INR(amt),    sTDgreen if amt else sTD),
                Paragraph(INR(cash)   if cash   else "—", sTDamber if cash   else sTD),
                Paragraph(INR(online) if online else "—", sTDteal  if online else sTD),
            ])
        col_w = [8*mm, 30*mm, 42*mm, 28*mm, 22*mm, 20*mm, 14*mm, 14*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",   (0,0),(-1,0),  BLUE),
            ("BACKGROUND",   (0,1),(-1,-1), WHITE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[WHITE, GREY_LT]),
            ("GRID",         (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",   (0,0),(-1,-1), 4),
            ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ])))
    else:
        els.append(Paragraph("No bookings in this period.", sSub2))

    # ── WALLET TRANSACTIONS TABLE ─────────────────────────────────────────────
    if wallet_txns:
        section_header(f"💰  WALLET TRANSACTIONS  ({len(wallet_txns)})")
        tdata = [[
            Paragraph("#", sTH), Paragraph("Type", sTH), Paragraph("Description", sTH),
            Paragraph("Ref ID", sTH), Paragraph("Date", sTH),
            Paragraph("Amount", sTHr), Paragraph("Balance After", sTHr),
        ]]
        for i, t in enumerate(wallet_txns):
            txn_type = t.get("type","—")
            amt      = t.get("amount") or 0
            bal_af   = t.get("balance_after") or 0
            tc = GREEN if txn_type == "CREDIT" else RED if txn_type in ("DEBIT","WITHDRAWAL") else GREY_DK
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph(txn_type, S(f"tt{i}", 8, tc, bold=True)),
                Paragraph((t.get("description") or "—")[:40], sTD),
                Paragraph((t.get("reference_id") or "—")[:20], sTD),
                Paragraph(iso_d(t.get("created_at")), sTD),
                Paragraph(INR(amt), S(f"ta{i}", 8, tc, bold=True, align=2)),
                Paragraph(INR(bal_af), sTDr),
            ])
        col_w = [8*mm, 22*mm, 54*mm, 26*mm, 22*mm, 22*mm, 24*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
            ("GRID",          (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",    (0,0),(-1,-1), 4),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ])))

    # ── WITHDRAWAL REQUESTS TABLE ─────────────────────────────────────────────
    if withdrawals:
        section_header(f"🏧  WITHDRAWAL REQUESTS  ({len(withdrawals)})")
        tdata = [[
            Paragraph("#", sTH), Paragraph("Amount", sTHr), Paragraph("Status", sTH),
            Paragraph("UPI / Bank", sTH), Paragraph("Payment Ref", sTH),
            Paragraph("Requested", sTH), Paragraph("Reviewed", sTH),
        ]]
        for i, wr in enumerate(withdrawals):
            amt    = wr.get("amount") or 0
            status = wr.get("status","—")
            sc = GREEN if status == "APPROVED" else RED if status == "REJECTED" else AMBER
            payment_dest = wr.get("upi_id") or wr.get("bank_account") or "—"
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph(INR(amt), S(f"wa{i}", 8, sc, bold=True, align=2)),
                Paragraph(status, S(f"ws{i}", 8, sc, bold=True)),
                Paragraph(str(payment_dest)[:24], sTD),
                Paragraph((wr.get("payment_reference") or "—")[:20], sTD),
                Paragraph(iso_d(wr.get("created_at")), sTD),
                Paragraph(iso_d(wr.get("reviewed_at")), sTD),
            ])
        col_w = [8*mm, 22*mm, 20*mm, 38*mm, 30*mm, 22*mm, 22*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
            ("GRID",          (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",    (0,0),(-1,-1), 4),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ])))

    # ── COMMISSIONS TABLE ─────────────────────────────────────────────────────
    if commissions:
        section_header(f"🏆  COMMISSIONS  ({len(commissions)})")
        tdata = [[
            Paragraph("#", sTH), Paragraph("Type", sTH), Paragraph("Item", sTH),
            Paragraph("Base Amt", sTHr), Paragraph("Commission", sTHr),
            Paragraph("Status", sTH), Paragraph("Source", sTH), Paragraph("Payout", sTH),
        ]]
        for i, c in enumerate(commissions):
            comm_amt = c.get("commission_amount") or 0
            status   = c.get("status","PENDING")
            sc = GREEN if status == "PAID" else TEAL if status == "APPROVED" else AMBER
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph((c.get("item_type") or "—"), sTD),
                Paragraph((c.get("item_name") or "—")[:30], sTD),
                Paragraph(INR(c.get("base_amount") or 0), sTDr),
                Paragraph(INR(comm_amt), S(f"ca{i}", 8, PURPLE, bold=True, align=2)),
                Paragraph(status, S(f"cs{i}", 8, sc, bold=True)),
                Paragraph((c.get("part_source") or "—"), sTD),
                Paragraph(iso_d(c.get("payout_date")), sTD),
            ])
        col_w = [8*mm, 18*mm, 42*mm, 20*mm, 22*mm, 20*mm, 26*mm, 22*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
            ("GRID",          (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",    (0,0),(-1,-1), 4),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ])))

    # ── QUOTATIONS TABLE ──────────────────────────────────────────────────────
    if quotations:
        section_header(f"📄  QUOTATIONS  ({len(quotations)})")
        tdata = [[
            Paragraph("#", sTH), Paragraph("Quotation No.", sTH),
            Paragraph("Status", sTH), Paragraph("Amount", sTHr), Paragraph("Date", sTH),
        ]]
        for i, q in enumerate(quotations):
            status = q.get("status","—")
            sc = GREEN if status in ("APPROVED","PAID") else RED if status == "REJECTED" else AMBER
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph(q.get("quotation_number","—"), sTDbold),
                Paragraph(status, S(f"qs{i}", 8, sc, bold=True)),
                Paragraph(INR(q.get("total_amount") or 0), sTDgreen),
                Paragraph(iso_d(q.get("created_at")), sTD),
            ])
        col_w = [8*mm, 50*mm, 35*mm, 35*mm, 50*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
            ("GRID",          (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",    (0,0),(-1,-1), 4),
        ])))

    # ── RATINGS ───────────────────────────────────────────────────────────────
    if ratings:
        section_header(f"⭐  RATINGS  ({len(ratings)})")
        tdata = [[Paragraph("#", sTH), Paragraph("Rating", sTH), Paragraph("Review", sTH), Paragraph("Date", sTH)]]
        for i, r in enumerate(ratings):
            stars = "★" * int(r.get("rating") or 0) + "☆" * (5 - int(r.get("rating") or 0))
            tdata.append([
                Paragraph(str(i+1), sTD),
                Paragraph(f"{stars}  ({r.get('rating','—')})", S(f"rs{i}", 8, ORANGE, bold=True)),
                Paragraph((r.get("review") or "No written review")[:60], sTD),
                Paragraph(iso_d(r.get("created_at")), sTD),
            ])
        col_w = [8*mm, 38*mm, 100*mm, 32*mm]
        els.append(Table(tdata, colWidths=col_w, style=TableStyle([
            ("BACKGROUND",    (0,0),(-1,0),  BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GREY_LT]),
            ("GRID",          (0,0),(-1,-1), 0.4, DIVIDER),
            ("ROWPADDING",    (0,0),(-1,-1), 4),
        ])))

    # ── FOOTER ────────────────────────────────────────────────────────────────
    els.append(Spacer(1, 8*mm))
    els.append(HRFlowable(width=W, thickness=0.5, color=DIVIDER))
    els.append(Spacer(1, 2*mm))
    els.append(Paragraph(
        f"Bibek Enterprises  •  Technician Performance Report  •  Computer-generated on {now_str}",
        sFooter
    ))

    doc.build(els)
    buf.seek(0)
    return buf.read()


# ─── GET /reports/technician-detail/pdf ─────────────────────────────────────
@router.get("/technician-detail/pdf", summary="Download technician report as PDF [Admin]")
async def technician_detail_report_pdf(
    technician_id: str = Query(..., description="Technician UUID"),
    period: str = Query("monthly", regex="^(weekly|monthly|yearly)$"),
    year: int = Query(None),
    month: int = Query(None),
    week: int = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates and streams a PDF version of the full technician detail report.
    Same data as /technician-detail JSON endpoint — bookings, quotations,
    payments, commissions, wallet, withdrawal requests, ratings.
    """
    from fastapi.responses import StreamingResponse
    from io import BytesIO

    # Re-use the JSON endpoint logic by calling it and extracting data.
    # technician_detail_report returns a plain dict via success_response(),
    # NOT a JSONResponse — so we access .get("data") directly.
    resp = await technician_detail_report(
        technician_id=technician_id,
        period=period,
        year=year,
        month=month,
        week=week,
        start_date=start_date,
        end_date=end_date,
        current_user=current_user,
        db=db,
    )
    report_data = resp.get("data", {})

    tech_name = report_data.get("technician", {}).get("name", "technician")
    period_str = f"{period}_{year or ''}{month or ''}{week or ''}"
    filename = f"report_{tech_name.replace(' ','_')}_{period_str}.pdf".lower()

    try:
        pdf_bytes = _build_technician_report_pdf(report_data)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Technician PDF generation failed")
        raise HTTPException(status_code=500, detail="PDF generation failed")

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

