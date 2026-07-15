from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import joinedload
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceType
from app.models.payment import PaymentStatus, PaymentTransaction


def _resolve_date_range(start_date: date | None, end_date: date | None):
    resolved_end = end_date or date.today()
    resolved_start = start_date or (resolved_end - timedelta(days=29))
    if resolved_start > resolved_end:
        raise ValueError("start_date must be before or equal to end_date")
    start_dt = datetime.combine(resolved_start, time.min)
    end_dt = datetime.combine(resolved_end, time.max)
    return resolved_start, resolved_end, start_dt, end_dt


async def build_gst_report(
    db: AsyncSession,
    start_date: date | None = None,
    end_date: date | None = None,
):
    resolved_start, resolved_end, start_dt, end_dt = _resolve_date_range(start_date, end_date)
    invoices = (
        await db.execute(
            select(Invoice).where(
                Invoice.is_active == True,
                Invoice.created_at >= start_dt,
                Invoice.created_at <= end_dt,
            )
        )
    ).scalars().all()

    summary_by_type = {
        InvoiceType.GST_B2C.value: {"invoice_count": 0, "taxable_amount": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 0.0},
        InvoiceType.GST_B2B.value: {"invoice_count": 0, "taxable_amount": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 0.0},
        InvoiceType.NON_GST.value: {"invoice_count": 0, "taxable_amount": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 0.0},
    }
    monthly = defaultdict(lambda: {"invoice_count": 0, "taxable_amount": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 0.0})
    totals = {"invoice_count": 0, "taxable_amount": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "total_amount": 0.0}

    for invoice in invoices:
        invoice_type = invoice.invoice_type.value
        bucket = summary_by_type[invoice_type]
        month_key = invoice.created_at.strftime("%Y-%m")
        month_bucket = monthly[month_key]
        for target in (bucket, month_bucket, totals):
            target["invoice_count"] += 1
            target["taxable_amount"] = int(round(target["taxable_amount"] + invoice.taxable_amount))
            target["cgst_amount"] = int(round(target["cgst_amount"] + invoice.cgst_amount))
            target["sgst_amount"] = int(round(target["sgst_amount"] + invoice.sgst_amount))
            target["igst_amount"] = int(round(target["igst_amount"] + invoice.igst_amount))
            target["total_amount"] = int(round(target["total_amount"] + invoice.total_amount))

    b2c = summary_by_type.get(InvoiceType.GST_B2C.value, {})
    b2b = summary_by_type.get(InvoiceType.GST_B2B.value, {})
    # Build line_items list — only GST invoices (not NON_GST)
    gst_invoices = [inv for inv in invoices if inv.invoice_type != InvoiceType.NON_GST]

    return {
        "date_range": {"start_date": resolved_start.isoformat(), "end_date": resolved_end.isoformat()},
        "totals": totals,
        "by_type": summary_by_type,
        "monthly": [{"month": month, **values} for month, values in sorted(monthly.items())],
        # Frontend-friendly flat keys
        "total_invoices": b2c.get("invoice_count", 0) + b2b.get("invoice_count", 0),
        "b2c_invoices": b2c.get("invoice_count", 0),
        "b2b_invoices": b2b.get("invoice_count", 0),
        "total_taxable": int(round(b2c.get("taxable_amount", 0) + b2b.get("taxable_amount", 0))),
        "total_cgst": int(round(b2c.get("cgst_amount", 0) + b2b.get("cgst_amount", 0))),
        "total_sgst": int(round(b2c.get("sgst_amount", 0) + b2b.get("sgst_amount", 0))),
        "total_igst": int(round(b2c.get("igst_amount", 0) + b2b.get("igst_amount", 0))),
        "total_tax": round(
            b2c.get("cgst_amount", 0) + b2c.get("sgst_amount", 0) + b2c.get("igst_amount", 0) +
            b2b.get("cgst_amount", 0) + b2b.get("sgst_amount", 0) + b2b.get("igst_amount", 0),
            2
        ),
        "line_items": [
            {
                "invoice_number": inv.invoice_number,
                "date": inv.created_at.isoformat() if inv.created_at else None,
                "customer_name": None,  # joined separately if needed
                "gstin": inv.gstin,
                "taxable_amount": int(round(inv.taxable_amount or 0)),
                "cgst": int(round(inv.cgst_amount or 0)),
                "sgst": int(round(inv.sgst_amount or 0)),
                "igst": int(round(inv.igst_amount or 0)),
                "total_tax": int(round((inv.cgst_amount or 0) + (inv.sgst_amount or 0) + (inv.igst_amount or 0))),
                "invoice_total": int(round(inv.total_amount or 0)),
                "type": inv.invoice_type.value if inv.invoice_type else "GST_B2C",
            }
            for inv in gst_invoices
        ],
    }


async def build_revenue_report(
    db: AsyncSession,
    start_date: date | None = None,
    end_date: date | None = None,
):
    resolved_start, resolved_end, start_dt, end_dt = _resolve_date_range(start_date, end_date)
    invoices = (
        await db.execute(
            select(Invoice).where(
                Invoice.is_active == True,
                Invoice.created_at >= start_dt,
                Invoice.created_at <= end_dt,
            )
        )
    ).scalars().all()
    payments = (
        await db.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.is_active == True,
                PaymentTransaction.status == PaymentStatus.SUCCESS,
                PaymentTransaction.created_at >= start_dt,
                PaymentTransaction.created_at <= end_dt,
            )
        )
    ).scalars().all()
    bookings = (
        await db.execute(
            select(Booking).where(
                Booking.is_active == True,
                Booking.created_at >= start_dt,
                Booking.created_at <= end_dt,
            )
        )
    ).scalars().all()

    daily = defaultdict(lambda: {"invoiced_amount": 0.0, "paid_amount": 0.0, "invoice_count": 0, "payment_count": 0})
    for invoice in invoices:
        day_key = invoice.created_at.date().isoformat()
        daily[day_key]["invoiced_amount"] = int(round(daily[day_key]["invoiced_amount"] + invoice.total_amount))
        daily[day_key]["invoice_count"] += 1
    for payment in payments:
        payment_dt = (payment.paid_at or payment.created_at).date().isoformat()
        daily[payment_dt]["paid_amount"] = int(round(daily[payment_dt]["paid_amount"] + payment.amount))
        daily[payment_dt]["payment_count"] += 1

    total_invoiced = int(round(sum(item.total_amount for item in invoices)))
    total_paid = int(round(sum(item.amount for item in payments)))
    total_outstanding = int(round(sum(item.balance_amount for item in invoices)))

    return {
        "date_range": {"start_date": resolved_start.isoformat(), "end_date": resolved_end.isoformat()},
        "summary": {
            "invoice_count": len(invoices),
            "booking_count": len(bookings),
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "total_outstanding": total_outstanding,
            "collection_rate": round((total_paid / total_invoiced) * 100, 2) if total_invoiced else 0.0,
        },
        "daily": [{"date": key, **value} for key, value in sorted(daily.items())],
        # Frontend-friendly flat keys (Reports.tsx expects these)
        "total_revenue": total_paid,
        "total_bookings": len(bookings),
        "completed_bookings": sum(1 for b in bookings if getattr(b, "status", "") in ("COMPLETED", "CLOSED", "SETTLED")),
        "average_booking_value": int(round(total_paid / len(bookings))) if bookings else 0.0,
        "breakdown": [{"date": key, "revenue": v["paid_amount"], "bookings": v["payment_count"], "avg_value": round(v["paid_amount"] / v["payment_count"], 2) if v["payment_count"] else 0} for key, v in sorted(daily.items())],
    }


async def build_customer_report(
    db: AsyncSession,
    start_date: date | None = None,
    end_date: date | None = None,
):
    resolved_start, resolved_end, start_dt, end_dt = _resolve_date_range(start_date, end_date)
    customers = (await db.execute(select(Customer).where(Customer.is_active == True))).scalars().all()
    bookings = (
        await db.execute(
            select(Booking).where(
                Booking.is_active == True,
                Booking.created_at >= start_dt,
                Booking.created_at <= end_dt,
            )
        )
    ).scalars().all()
    invoices = (
        await db.execute(
            select(Invoice, Booking.customer_id)
            .join(Booking, Booking.id == Invoice.booking_id)
            .where(
                Invoice.is_active == True,
                Invoice.created_at >= start_dt,
                Invoice.created_at <= end_dt,
            )
        )
    ).all()
    payments = (
        await db.execute(
            select(PaymentTransaction, Booking.customer_id)
            .join(Booking, Booking.id == PaymentTransaction.booking_id)
            .where(
                PaymentTransaction.is_active == True,
                PaymentTransaction.status == PaymentStatus.SUCCESS,
                PaymentTransaction.created_at >= start_dt,
                PaymentTransaction.created_at <= end_dt,
            )
        )
    ).all()

    aggregates = {
        customer.id: {
            "customer_id": str(customer.id),
            "name": customer.name,
            "mobile": customer.mobile,
            "booking_count": 0,
            "invoice_amount": 0.0,
            "paid_amount": 0.0,
        }
        for customer in customers
    }

    for booking in bookings:
        if booking.customer_id in aggregates:
            aggregates[booking.customer_id]["booking_count"] += 1
    for invoice, customer_id in invoices:
        if customer_id in aggregates:
            aggregates[customer_id]["invoice_amount"] = int(round(aggregates[customer_id]["invoice_amount"] + invoice.total_amount))
    for payment, customer_id in payments:
        if customer_id in aggregates:
            aggregates[customer_id]["paid_amount"] = int(round(aggregates[customer_id]["paid_amount"] + payment.amount))

    ranked = sorted(
        aggregates.values(),
        key=lambda item: (item["paid_amount"], item["invoice_amount"], item["booking_count"]),
        reverse=True,
    )
    new_customers = [customer for customer in customers if start_dt <= customer.created_at <= end_dt]
    active_customers = [item for item in ranked if item["booking_count"] > 0 or item["paid_amount"] > 0]

    return {
        "date_range": {"start_date": resolved_start.isoformat(), "end_date": resolved_end.isoformat()},
        "summary": {
            "total_customers": len(customers),
            "new_customers": len(new_customers),
            "active_customers": len(active_customers),
        },
        "top_customers": ranked[:10],
    }


def build_placeholder_report(module: str, reason: str):
    return {
        "summary": {
            "module": module,
            "records_available": 0,
            "source_module_ready": False,
        },
        "items": [],
        "reason": reason,
    }
