"""
Chatbot + Callback Request API
Endpoints:
  POST /chatbot/message          — main NLP chat endpoint (public, no auth required)
  POST /chatbot/callback         — save callback request (public, no auth required)
  GET  /chatbot/callback-requests — list callback requests [Admin/CCO]
  GET  /chatbot/callback-requests/{id} — detail with customer lookup [Admin/CCO]
  PUT  /chatbot/callback-requests/{id} — update status / notes [Admin/CCO]
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from typing import Optional
from app.utils.timezone import now_ist, now_naive
from datetime import datetime, timezone
from pydantic import BaseModel as PydanticModel
import httpx

from app.core.database import get_db
from app.models.callback_request import CallbackRequest, CallbackStatus
from app.models.customer import Customer, CustomerAddress
from app.models.booking import Booking, BookingStatus
from app.models.service import Service, ServiceCategory
from app.models.city import City
from app.models.user import User
from app.models.domain import Domain, DomainProfile
from app.api.deps import AdminOrCCO
from app.utils.response import success_response

router = APIRouter()

DOMAIN_SLUG = "bibekenterprises"

# ── Schemas ──────────────────────────────────────────────────────────────────

class ChatMessageRequest(PydanticModel):
    message: str
    session_id: Optional[str] = None
    domain_slug: Optional[str] = DOMAIN_SLUG

class CallbackCreateRequest(PydanticModel):
    mobile: str
    name: Optional[str] = None
    message: Optional[str] = None
    source: str = "CHATBOT"
    page_url: Optional[str] = None
    domain_slug: Optional[str] = None

class CallbackUpdateRequest(PydanticModel):
    status: Optional[str] = None
    admin_notes: Optional[str] = None
    # called_at is set server-side automatically when status=CALLED


# ── NLP Intent Engine ─────────────────────────────────────────────────────────

INTENT_KEYWORDS = {
    "greeting":         ["hi", "hello", "hey", "namaste", "good morning", "good evening", "good afternoon", "helo", "hii", "hai"],
    "services":         ["service", "services", "repair", "fix", "what do you", "what you do", "offer", "provide", "servi", "sevice", "repar"],
    "booking":          ["book", "booking", "appointment", "schedule", "appoint", "buk", "boking", "schedul"],
    "booking_status":   ["status", "my booking", "where", "track", "update", "statu", "satatus", "bookig status"],
    "contact":          ["contact", "phone", "address", "location", "reach", "call", "contac", "adress", "whatsapp"],
    "callback":         ["callback", "call back", "call me", "ring me", "please call", "cal back"],
    "pricing":          ["price", "cost", "charge", "rate", "fee", "how much", "pric", "prce"],
    "cancel":           ["cancel", "cancell", "cancle"],
    "goodbye":          ["bye", "thanks", "thank you", "ok", "okay", "done", "exit", "quit"],
    "cities":           ["city", "cities", "area", "areas", "service area", "service areas", "location", "where", "available", "operate", "coverage"],
}

def detect_intent(text: str) -> str:
    t = text.lower().strip()
    scores = {intent: 0 for intent in INTENT_KEYWORDS}
    for intent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                scores[intent] += 1
    best = max(scores, key=lambda i: scores[i])
    return best if scores[best] > 0 else "unknown"

def fuzzy_match_service(text: str, services: list) -> list:
    """Return top matches from services list based on name similarity."""
    t = text.lower()
    matches = []
    for s in services:
        name = s["name"].lower()
        # simple substring or token overlap
        tokens = name.split()
        if any(tok in t or t in tok for tok in tokens) or name in t:
            matches.append(s)
    return matches[:3]


# ── Main Chat Endpoint ────────────────────────────────────────────────────────

@router.post("/message", summary="Chatbot NLP message handler [Public]")
async def chat_message(payload: ChatMessageRequest, db: AsyncSession = Depends(get_db)):
    intent = detect_intent(payload.message)

    # ── Load domain + domain_profile for real contact details ──────────────────
    domain = (await db.execute(select(Domain).where(Domain.slug == DOMAIN_SLUG))).scalar_one_or_none()
    profile = None
    if domain:
        profile = (await db.execute(
            select(DomainProfile).where(DomainProfile.domain_id == domain.id)
        )).scalar_one_or_none()

    company_name    = (domain.name               if domain and domain.name               else "Bibek Enterprises")
    company_phone   = (profile.support_phone     if profile and profile.support_phone    else "+91 98765 43210")
    wa_raw          = (profile.whatsapp_number   if profile and profile.whatsapp_number  else "")
    wa_digits = "".join(c for c in wa_raw if c.isdigit())
    if not wa_digits:
        company_whatsapp = "https://wa.me/919876543210"
    elif wa_digits.startswith("91") and len(wa_digits) == 12:
        company_whatsapp = f"https://wa.me/{wa_digits}"   # already has country code
    else:
        company_whatsapp = f"https://wa.me/91{wa_digits}"  # prepend India country code
    addr_parts      = [p for p in [
                            profile.office_address if profile else None,
                            profile.office_city    if profile else None,
                            profile.office_state   if profile else None,
                       ] if p]
    company_address = ", ".join(addr_parts) if addr_parts else "Bhubaneswar, Odisha, India"

    # ── Greeting ──────────────────────────────────────────────────────────────
    if intent == "greeting":
        return success_response(data={
            "intent": intent,
            "reply": f"👋 Hello! Welcome to **{company_name}**!\n\nI'm your smart assistant. I can help you with:\n\n"
                     "• 🔧 Book a repair/service\n"
                     "• 📋 Check your booking status\n"
                     "• ℹ️ Service information & pricing\n"
                     "• 📞 Contact details\n"
                     "• 🏙️ Service locations\n\n"
                     "What would you like to do today?",
            "quick_replies": ["Book a Service", "My Booking Status", "Our Services", "Contact Us", "Service Areas"]
        })

    # ── Services list ────────────────────────────────────────────────────────
    if intent == "services":
        # domain already loaded above
        services = []
        if domain:
            from app.models.domain import DomainService
            rows = await db.execute(
                select(Service, ServiceCategory)
                .join(DomainService, DomainService.service_id == Service.id)
                .join(ServiceCategory, ServiceCategory.id == Service.category_id)
                .where(DomainService.domain_id == domain.id, Service.is_active == True)
                .order_by(ServiceCategory.name)
                .limit(20)
            )
            for svc, cat in rows.all():
                services.append({
                    "id": str(svc.id),
                    "name": svc.name,
                    "category": cat.name,
                    "slug": svc.slug if hasattr(svc, "slug") else None,
                    "base_price": svc.base_price,
                })

        if not services:
            reply = "We offer a wide range of home appliance repair & maintenance services! Visit our services page for details."
            service_cards = []
        else:
            # Group by category
            cats: dict = {}
            for s in services:
                cats.setdefault(s["category"], []).append(s)
            lines = []
            for cat, svcs in cats.items():
                lines.append(f"**{cat}**")
                for s in svcs:
                    lines.append(f"  • {s['name']} — ₹{s['base_price']:.0f}")
            reply = "Here are our services:\n\n" + "\n".join(lines) + "\n\nClick any service below to view details or book now!"
            service_cards = services

        return success_response(data={
            "intent": intent,
            "reply": reply,
            "services": service_cards,
            "quick_replies": ["Book a Service", "Service Pricing", "Back to Menu"]
        })

    # ── Pricing ───────────────────────────────────────────────────────────────
    if intent == "pricing":
        return success_response(data={
            "intent": intent,
            "reply": "💰 Our service charges depend on the type of repair and appliance. "
                     "Inspection charges start from **₹199**.\n\n"
                     "Final pricing is given after technician inspection.\n\n"
                     "Would you like to book a service or see our full service list?",
            "quick_replies": ["Book a Service", "Our Services", "Contact Us"]
        })

    # ── Cities/Areas ──────────────────────────────────────────────────────────
    if intent == "cities":
        # domain already loaded above
        city_names = []
        if domain:
            from app.models.domain import DomainCity
            rows = await db.execute(
                select(City).join(DomainCity, DomainCity.city_id == City.id)
                .where(DomainCity.domain_id == domain.id, City.is_active == True)
                .order_by(City.name)
            )
            city_names = [c.name for c in rows.scalars().all()]

        if city_names:
            reply = f"🏙️ We currently operate in:\n\n" + "\n".join(f"• {c}" for c in city_names)
        else:
            reply = "🏙️ We serve multiple cities across Odisha. Contact us to confirm availability in your area!"

        return success_response(data={
            "intent": intent,
            "reply": reply,
            "quick_replies": ["Book a Service", "Contact Us", "Back to Menu"]
        })

    # ── Contact ───────────────────────────────────────────────────────────────
    if intent == "contact":
        return success_response(data={
            "intent": intent,
            "reply": f"📞 **Contact {company_name}**\n\n"
                     f"📱 Phone: {company_phone}\n"
                     f"💬 WhatsApp: [Chat Now]({company_whatsapp})\n"
                     f"📍 Address: {company_address}\n\n"
                     "We're available **Mon–Sat, 9 AM – 7 PM**",
            "contact": {
                "phone": company_phone,
                "whatsapp": company_whatsapp,
                "address": company_address,
            },
            "quick_replies": ["Book a Service", "Request Callback", "Back to Menu"]
        })

    # ── Booking status ─────────────────────────────────────────────────────────
    if intent == "booking_status":
        return success_response(data={
            "intent": intent,
            "reply": "📋 To check your booking status, please **log in** first so I can show your bookings securely.\n\n"
                     "Or type your **booking number** (e.g. BK12345678) and I'll look it up!",
            "action": "ask_booking_number",
            "quick_replies": ["Login to Check", "Enter Booking Number", "Back to Menu"]
        })

    # ── Callback ──────────────────────────────────────────────────────────────
    if intent == "callback":
        return success_response(data={
            "intent": intent,
            "reply": "📞 Sure! Please share your **mobile number** and we'll call you back shortly.",
            "action": "collect_callback_mobile",
            "quick_replies": []
        })

    # ── Cancel ────────────────────────────────────────────────────────────────
    if intent == "cancel":
        return success_response(data={
            "intent": intent,
            "reply": "❌ To cancel a booking, please log in and go to **My Bookings**, "
                     "or contact us directly and we'll help you cancel.\n\n"
                     f"📞 Call: {company_phone}",
            "quick_replies": ["Contact Us", "Back to Menu"]
        })

    # ── Booking intent ────────────────────────────────────────────────────────
    if intent == "booking":
        return success_response(data={
            "intent": intent,
            "reply": "🔧 Great! Let's book a service for you.\n\n"
                     "I'll check if you're logged in first. If not, I'll verify your mobile number quickly via OTP.",
            "action": "start_booking",
            "quick_replies": ["Start Booking", "View Services First"]
        })

    # ── Goodbye ───────────────────────────────────────────────────────────────
    if intent == "goodbye":
        return success_response(data={
            "intent": intent,
            "reply": f"😊 Thank you for contacting **{company_name}**! Have a great day!\n\n"
                     "Feel free to chat again anytime. 👋",
            "quick_replies": []
        })

    # ── Booking number lookup ─────────────────────────────────────────────────
    import re
    bk_match = re.search(r"BK\d{6,10}", payload.message.upper())
    if bk_match:
        bk_number = bk_match.group()
        booking = (await db.execute(
            select(Booking).where(Booking.booking_number == bk_number)
        )).scalar_one_or_none()
        if booking:
            # Resolve service name: prefer FK-joined name, fall back to free-text field
            svc_name = booking.service_name
            if not svc_name and booking.service_id:
                svc_row = (await db.execute(select(Service).where(Service.id == booking.service_id))).scalar_one_or_none()
                if svc_row:
                    svc_name = svc_row.name
            status_map = {
                "PENDING": "⏳ Pending confirmation",
                "CONFIRMED": "✅ Confirmed",
                "ASSIGNED": "👨‍🔧 Technician assigned",
                "ACCEPTED": "👨‍🔧 Technician accepted",
                "EN_ROUTE": "🚗 Technician on the way",
                "ARRIVED": "📍 Technician arrived",
                "IN_PROGRESS": "🔧 Work in progress",
                "WORK_STARTED": "🔧 Work started",
                "COMPLETED": "✅ Completed",
                "CLOSED": "✅ Closed",
                "SETTLED": "✅ Settled",
                "CANCELLED": "❌ Cancelled",
                "RESCHEDULED": "🔄 Rescheduled",
                "PAID": "💳 Paid",
                "INVOICE_GENERATED": "🧾 Invoice generated",
            }
            status_label = status_map.get(booking.status.value, booking.status.value)
            address_display = booking.address_line or booking.city or "Saved address"
            reply = (f"📋 **Booking #{bk_number}**\n\n"
                     f"Status: {status_label}\n"
                     f"Service: {svc_name or '—'}\n"
                     f"Date: {booking.scheduled_date.strftime('%d %b %Y') if booking.scheduled_date else 'TBD'}\n"
                     f"Slot: {booking.scheduled_slot or 'TBD'}\n"
                     f"Address: {address_display}")
        else:
            reply = f"❌ No booking found with number **{bk_number}**. Please double-check the number."
        return success_response(data={"intent": "booking_status", "reply": reply,
                                      "quick_replies": ["My Booking Status", "Book a Service", "Back to Menu"]})

    # ── Try to fuzzy match to a service ──────────────────────────────────────
    # domain already loaded at the top of the handler
    matched_services = []
    if domain:
        from app.models.domain import DomainService
        rows = await db.execute(
            select(Service).join(DomainService, DomainService.service_id == Service.id)
            .where(DomainService.domain_id == domain.id, Service.is_active == True)
        )
        all_services = [{"id": str(s.id), "name": s.name} for s in rows.scalars().all()]
        matched_services = fuzzy_match_service(payload.message, all_services)

    if matched_services:
        if len(matched_services) == 1:
            s = matched_services[0]
            return success_response(data={
                "intent": "service_detail",
                "reply": f"🔧 Did you mean **{s['name']}**?\n\nWould you like to book this service?",
                "services": matched_services,
                "quick_replies": [f"Book {s['name']}", "View All Services", "Back to Menu"]
            })
        else:
            names = "\n".join(f"• {s['name']}" for s in matched_services)
            return success_response(data={
                "intent": "service_detail",
                "reply": f"🔍 I found a few services that might match:\n\n{names}\n\nWhich one are you looking for?",
                "services": matched_services,
                "quick_replies": [s["name"] for s in matched_services] + ["View All Services"]
            })

    # ── Fallback ──────────────────────────────────────────────────────────────
    return success_response(data={
        "intent": "unknown",
        "reply": "🤔 I'm not sure I understood that. Here's what I can help you with:",
        "quick_replies": ["Book a Service", "My Booking Status", "Our Services", "Contact Us", "Request Callback"]
    })


# ── Callback Request endpoints ────────────────────────────────────────────────

def _client_ip(request: Request) -> Optional[str]:
    """Best-effort real client IP, respecting a reverse proxy's
    X-Forwarded-For header (set by nginx/Cloudflare in production) and
    falling back to the direct connection IP in dev."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


async def _geolocate_ip(ip: Optional[str]) -> Optional[str]:
    """Looks up an approximate city/region/country for an IP address using
    ip-api.com's free tier (no key required). Best-effort only — used so
    an admin has *some* context on an unknown lead before calling; never
    blocks or fails the callback request if the lookup is slow/unavailable.
    Skips lookup entirely for localhost/private IPs (local dev)."""
    if not ip or ip in ("127.0.0.1", "localhost", "::1") or ip.startswith(("10.", "192.168.", "172.")):
        return None
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,city,regionName,country"},
            )
            data = resp.json()
            if data.get("status") == "success":
                parts = [p for p in [data.get("city"), data.get("regionName"), data.get("country")] if p]
                return ", ".join(parts) if parts else None
    except Exception:
        pass
    return None


@router.post("/callback", summary="Save callback request [Public]")
async def create_callback(
    payload: CallbackCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Look up an existing customer by mobile first — if found, we don't
    # need to guess anything about them from IP/location.
    cust = (await db.execute(select(Customer).where(Customer.mobile == payload.mobile))).scalar_one_or_none()

    ip_address = None
    user_agent = None
    location = None
    domain_id = None

    if not cust:
        # Unknown lead — capture visitor context so the admin has something
        # to go on before calling. Never blocks the save if lookups fail.
        ip_address = _client_ip(request)
        user_agent = request.headers.get("user-agent")
        location = await _geolocate_ip(ip_address)

    if payload.domain_slug:
        dom = (await db.execute(select(Domain).where(Domain.slug == payload.domain_slug))).scalar_one_or_none()
        domain_id = dom.id if dom else None

    cb = CallbackRequest(
        mobile=payload.mobile,
        name=payload.name,
        message=payload.message,
        source=payload.source,
        page_url=payload.page_url,
        domain_id=domain_id,
        ip_address=ip_address,
        user_agent=user_agent,
        location=location,
    )
    db.add(cb)
    await db.commit()
    await db.refresh(cb)
    return success_response(data={"id": str(cb.id), "mobile": cb.mobile}, message="Callback request saved. We'll call you shortly!")


@router.get("/callback-requests", summary="List callback requests [Admin/CCO]")
async def list_callback_requests(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, le=200),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    q = select(CallbackRequest).order_by(CallbackRequest.created_at.desc())
    if status:
        q = q.where(CallbackRequest.status == CallbackStatus(status))
    if search:
        q = q.where(
            (CallbackRequest.mobile.ilike(f"%{search}%")) |
            (CallbackRequest.name.ilike(f"%{search}%"))
        )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.offset(skip).limit(limit))).scalars().all()

    items = []
    for cb in rows:
        # Quick customer lookup
        cust = (await db.execute(select(Customer).where(Customer.mobile == cb.mobile))).scalar_one_or_none()
        items.append({
            "id": str(cb.id),
            "mobile": cb.mobile,
            "name": cb.name,
            "message": cb.message,
            "source": cb.source,
            "status": cb.status.value,
            "admin_notes": cb.admin_notes,
            "called_at": str(cb.called_at) if cb.called_at else None,
            "created_at": str(cb.created_at),
            "has_customer": cust is not None,
            "customer_id": str(cust.id) if cust else None,
            "customer_name": cust.name if cust else None,
            "page_url": cb.page_url,
            "ip_address": cb.ip_address,
            "location": cb.location,
        })

    return success_response(data={"items": items, "total": total, "skip": skip, "limit": limit})


@router.get("/callback-requests/{cb_id}", summary="Callback request detail [Admin/CCO]")
async def get_callback_detail(
    cb_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    cb = (await db.execute(select(CallbackRequest).where(CallbackRequest.id == cb_id))).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, "Callback request not found")

    # Look up customer by mobile
    cust = (await db.execute(select(Customer).where(Customer.mobile == cb.mobile))).scalar_one_or_none()
    customer_data = None
    last_bookings = []
    if cust:
        # Last 5 bookings
        b_rows = (await db.execute(
            select(Booking).where(Booking.customer_id == cust.id)
            .order_by(Booking.created_at.desc()).limit(5)
        )).scalars().all()
        last_bookings = [{
            "id": str(b.id),
            "booking_number": b.booking_number,
            "service_name": b.service_name,
            "status": b.status.value,
            "scheduled_date": str(b.scheduled_date) if b.scheduled_date else None,
            "total_amount": b.total_amount,
        } for b in b_rows]

        # Addresses
        addrs = (await db.execute(
            select(CustomerAddress).where(CustomerAddress.customer_id == cust.id)
        )).scalars().all()
        customer_data = {
            "id": str(cust.id),
            "name": cust.name,
            "mobile": cust.mobile,
            "email": cust.email,
            "customer_code": cust.customer_code,
            "total_bookings": cust.total_bookings,
            "addresses": [{
                "id": str(a.id),
                "label": a.label,
                "address_line1": a.address_line1,
                "city": a.city,
                "state": a.state,
                "pincode": a.pincode,
                "is_default": a.is_default,
            } for a in addrs],
            "last_bookings": last_bookings,
        }

    return success_response(data={
        "id": str(cb.id),
        "mobile": cb.mobile,
        "name": cb.name,
        "message": cb.message,
        "source": cb.source,
        "status": cb.status.value,
        "admin_notes": cb.admin_notes,
        "called_at": str(cb.called_at) if cb.called_at else None,
        "created_at": str(cb.created_at),
        "customer": customer_data,
        # Visitor context — only meaningful when "customer" above is null,
        # i.e. this mobile number isn't a known customer yet.
        "page_url": cb.page_url,
        "ip_address": cb.ip_address,
        "user_agent": cb.user_agent,
        "location": cb.location,
    })


@router.put("/callback-requests/{cb_id}", summary="Update callback request [Admin/CCO]")
async def update_callback(
    cb_id: UUID,
    payload: CallbackUpdateRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    cb = (await db.execute(select(CallbackRequest).where(CallbackRequest.id == cb_id))).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, "Callback request not found")

    new_status = None
    if payload.status:
        try:
            new_status = CallbackStatus(payload.status)
        except ValueError:
            raise HTTPException(400, f"Invalid status: {payload.status!r}. Must be one of: PENDING, CALLED, RESOLVED, SKIPPED")
        cb.status = new_status

    if payload.admin_notes is not None:
        cb.admin_notes = payload.admin_notes

    # Set called_at server-side (naive UTC) when transitioning to CALLED.
    # Never trust a client-supplied datetime to avoid timezone offset errors.
    if new_status == CallbackStatus.CALLED and cb.called_at is None:
        cb.called_at = now_naive()  # naive UTC for TIMESTAMP WITHOUT TIME ZONE

    await db.commit()
    await db.refresh(cb)
    return success_response(
        data={
            "id": str(cb.id),
            "status": cb.status.value,
            "admin_notes": cb.admin_notes,
            "called_at": cb.called_at.isoformat() if cb.called_at else None,
        },
        message="Callback request updated",
    )
