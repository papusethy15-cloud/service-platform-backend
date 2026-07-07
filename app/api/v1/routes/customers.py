from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, update
from uuid import UUID
from typing import Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.models.customer import Customer, CustomerAddress
from app.models.user import User, UserRole
from app.api.v1.schemas.customer import (
    CreateCustomerRequest, UpdateCustomerRequest, CustomerAddressRequest
)
from app.api.deps import get_current_user, AdminOnly, AdminOrCCO
from app.utils.response import success_response
from app.utils.phone import normalize_mobile
import random, string

router = APIRouter()

def generate_customer_code():
    return "CUS" + ''.join(random.choices(string.digits, k=6))

async def _get_or_404_owned_customer(customer_id: UUID, current_user: dict, db: AsyncSession) -> Customer:
    """Fetch a customer row, enforcing that a CUSTOMER-role user can only access their own record."""
    customer = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if current_user["role"] == "CUSTOMER" and str(customer.user_id) != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return customer

# == SELF-SERVICE: get-or-create my own Customer profile [Website/App] ==
@router.get("/me", summary="Get (or auto-create) my own customer profile [Customer self-service]")
async def get_or_create_my_customer(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user["role"] != "CUSTOMER":
        raise HTTPException(status_code=403, detail="Only customer accounts have a customer profile")
    user_id = UUID(current_user["user_id"])
    customer = (await db.execute(select(Customer).where(Customer.user_id == user_id))).scalar_one_or_none()
    if not customer:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        customer = Customer(
            user_id=user_id,
            name=user.name or "New Customer",
            mobile=user.mobile,
            email=user.email,
            customer_code=generate_customer_code(),
        )
        db.add(customer)
        await db.commit()
        await db.refresh(customer)
    return success_response(data={
        "id": str(customer.id), "name": customer.name, "mobile": customer.mobile,
        "email": customer.email, "alternate_mobile": customer.alternate_mobile,
        "customer_code": customer.customer_code, "notes": customer.notes,
        "total_bookings": customer.total_bookings, "created_at": str(customer.created_at),
        "gst_number": customer.gst_number, "gst_name": customer.gst_name,
        "gst_address": customer.gst_address,
    })

@router.put("/me", summary="Update my own customer profile [Customer self-service]")
async def update_my_customer(payload: UpdateCustomerRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user["role"] != "CUSTOMER":
        raise HTTPException(status_code=403, detail="Only customer accounts have a customer profile")
    user_id = UUID(current_user["user_id"])
    customer = (await db.execute(select(Customer).where(Customer.user_id == user_id))).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer profile not found -- call GET /customers/me first")
    for f, v in payload.model_dump(exclude_none=True).items():
        setattr(customer, f, v)
    await db.commit()
    return success_response(message="Profile updated successfully")

# PATCH alias -- the customer apps call PATCH for partial profile updates,
# while PUT is kept for backwards compatibility with anything already using it.
@router.patch("/me", summary="Update my own customer profile (partial) [Customer self-service]")
async def patch_my_customer(payload: UpdateCustomerRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await update_my_customer(payload, current_user, db)


async def _get_my_customer(current_user: dict, db: AsyncSession) -> Customer:
    """Resolve the Customer row owned by the currently logged-in user."""
    if current_user["role"] != "CUSTOMER":
        raise HTTPException(status_code=403, detail="Only customer accounts have a customer profile")
    user_id = UUID(current_user["user_id"])
    customer = (await db.execute(select(Customer).where(Customer.user_id == user_id))).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer profile not found -- call GET /customers/me first")
    return customer

# == SELF-SERVICE: my own addresses [Customer App] ==
# NOTE: these MUST stay registered before the "/{customer_id}/addresses"
# block below, since "me" would otherwise be swallowed by the UUID path
# param and fail UUID parsing with a 422 before ever reaching that code.
@router.get("/me/addresses", summary="List my own addresses [Customer self-service]")
async def list_my_addresses(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    customer = await _get_my_customer(current_user, db)
    addresses = (await db.execute(
        select(CustomerAddress).where(CustomerAddress.customer_id == customer.id, CustomerAddress.is_active == True)
    )).scalars().all()
    return success_response(data=[{"id": str(a.id), "label": a.label, "address_line1": a.address_line1,
        "address_line2": a.address_line2, "city": a.city, "state": a.state,
        "pincode": a.pincode, "latitude": a.latitude, "longitude": a.longitude,
        "is_default": a.is_default} for a in addresses])

@router.post("/me/addresses", summary="Add my own address [Customer self-service]")
async def add_my_address(payload: CustomerAddressRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    customer = await _get_my_customer(current_user, db)
    if payload.is_default:
        for addr in (await db.execute(select(CustomerAddress).where(CustomerAddress.customer_id == customer.id, CustomerAddress.is_default == True))).scalars().all():
            addr.is_default = False
    loc_src_me = payload.location_source
    if not loc_src_me:
        if payload.latitude and payload.longitude:
            loc_src_me = "gps"
        else:
            loc_src_me = "manual"
    address = CustomerAddress(customer_id=customer.id, label=payload.label,
        address_line1=payload.address_line1, address_line2=payload.address_line2,
        city=payload.city, state=payload.state, pincode=payload.pincode,
        latitude=payload.latitude, longitude=payload.longitude,
        is_default=payload.is_default, location_source=loc_src_me)
    db.add(address)
    await db.flush()
    await db.commit()
    return success_response(data={"id": str(address.id)}, message="Address added successfully")

@router.put("/me/addresses/{address_id}", summary="Update my own address [Customer self-service]")
async def update_my_address(address_id: UUID, payload: CustomerAddressRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    customer = await _get_my_customer(current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer.id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    address.label = payload.label; address.address_line1 = payload.address_line1
    address.address_line2 = payload.address_line2; address.city = payload.city
    address.state = payload.state; address.pincode = payload.pincode
    address.latitude = payload.latitude; address.longitude = payload.longitude
    if payload.is_default and not address.is_default:
        for addr in (await db.execute(select(CustomerAddress).where(CustomerAddress.customer_id == customer.id, CustomerAddress.is_default == True))).scalars().all():
            addr.is_default = False
    address.is_default = payload.is_default
    await db.commit()
    return success_response(message="Address updated successfully")

@router.delete("/me/addresses/{address_id}", summary="Delete my own address [Customer self-service]")
async def delete_my_address(address_id: UUID, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    customer = await _get_my_customer(current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer.id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    address.is_active = False
    await db.commit()
    return success_response(message="Address deleted successfully")

@router.patch("/me/addresses/{address_id}/set-default", summary="Set my default address [Customer self-service]")
async def set_my_default_address(address_id: UUID, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    customer = await _get_my_customer(current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer.id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    for addr in (await db.execute(select(CustomerAddress).where(CustomerAddress.customer_id == customer.id, CustomerAddress.is_default == True))).scalars().all():
        addr.is_default = False
    address.is_default = True
    await db.commit()
    return success_response(message="Default address updated")


@router.put("/me/fcm-token", summary="Register or update FCM push token [Customer self-service]")
async def update_customer_fcm_token(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the Flutter customer app on login and whenever FirebaseMessaging
    issues a new token. Saves it on the customer row so the backend can push
    booking/quotation/payment notifications to this device.
    """
    token = (payload.get("fcm_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="fcm_token is required")
    customer = await _get_my_customer(current_user, db)
    customer.fcm_token = token
    await db.commit()
    return success_response(message="FCM token registered")


@router.get("/check-mobile/{mobile}", summary="Check if customer exists by mobile [Admin/CCO]")
async def check_customer_by_mobile(mobile: str, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    mobile = normalize_mobile(mobile)
    customer = (await db.execute(select(Customer).where(Customer.mobile == mobile, Customer.is_active == True))).scalar_one_or_none()
    if not customer:
        return success_response(data=None, message="Customer not found")
    return success_response(data={
        "id": str(customer.id), "name": customer.name, "mobile": customer.mobile,
        "email": customer.email, "alternate_mobile": customer.alternate_mobile,
        "customer_code": customer.customer_code, "notes": customer.notes,
        "total_bookings": customer.total_bookings, "created_at": str(customer.created_at),
        "gst_number": customer.gst_number, "gst_name": customer.gst_name,
        "gst_address": customer.gst_address,
    }, message="Customer found")

@router.get("", summary="List all customers [Admin/CCO]")
async def list_customers(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: str = Query(None),
    city: str = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    query = select(Customer).where(Customer.is_active == True)
    if search:
        query = query.where(Customer.name.ilike(f"%{search}%") | Customer.mobile.ilike(f"%{search}%"))
    if city:
        query = query.join(User, Customer.user_id == User.id).where(User.city == city)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar()
    customers = (await db.execute(query.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{"id": str(c.id), "name": c.name, "mobile": c.mobile, "email": c.email,
                   "customer_code": c.customer_code, "total_bookings": c.total_bookings,
                   "created_at": str(c.created_at)} for c in customers],
        "total": total, "page": page, "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    })

@router.post("", summary="Create customer [Admin/CCO]")
async def create_customer(
    payload: CreateCustomerRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    existing = (await db.execute(select(Customer).where(Customer.mobile == payload.mobile))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Customer with this mobile already exists")
    user = User(name=payload.name, mobile=payload.mobile, email=payload.email,
                role=UserRole.CUSTOMER, is_verified=True)
    db.add(user)
    await db.flush()
    customer = Customer(
        user_id=user.id, name=payload.name, mobile=payload.mobile,
        email=payload.email, alternate_mobile=payload.alternate_mobile,
        notes=payload.notes, customer_code=generate_customer_code()
    )
    db.add(customer)
    await db.flush()
    await db.commit()  # BUG FIX: missing commit
    return success_response(data={"id": str(customer.id), "customer_code": customer.customer_code,
                                   "name": customer.name, "mobile": customer.mobile},
                             message="Customer created successfully")

@router.get("/{customer_id}", summary="Get customer details [Admin/CCO]")
async def get_customer(customer_id: UUID, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Customer).where(Customer.id == customer_id, Customer.is_active == True))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return success_response(data={
        "id": str(customer.id), "name": customer.name, "mobile": customer.mobile,
        "email": customer.email, "alternate_mobile": customer.alternate_mobile,
        "customer_code": customer.customer_code, "notes": customer.notes,
        "total_bookings": customer.total_bookings, "created_at": str(customer.created_at),
        "gst_number": customer.gst_number, "gst_name": customer.gst_name,
        "gst_address": customer.gst_address,
    })

@router.put("/{customer_id}", summary="Update customer [Admin/CCO]")
async def update_customer(customer_id: UUID, payload: UpdateCustomerRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.name:             customer.name = payload.name
    if payload.email:            customer.email = payload.email
    if payload.alternate_mobile: customer.alternate_mobile = payload.alternate_mobile
    if payload.notes is not None: customer.notes = payload.notes
    if payload.gst_number is not None: customer.gst_number = payload.gst_number or None
    if payload.gst_name is not None:   customer.gst_name   = payload.gst_name or None
    if payload.gst_address is not None: customer.gst_address = payload.gst_address or None
    await db.commit()  # BUG FIX: missing commit
    return success_response(message="Customer updated successfully")

@router.delete("/{customer_id}", summary="Delete customer [Admin]")
async def delete_customer(customer_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.is_active = False
    await db.commit()  # BUG FIX: missing commit
    return success_response(message="Customer deactivated successfully")


@router.delete(
    "/{customer_id}/permanent",
    summary="Permanently delete a customer AND all related records (bookings, quotations, invoices, "
            "payments, warranties, ratings, etc.) [Admin only]",
)
async def permanently_delete_customer(
    customer_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Hard-deletes a Customer + everything that hangs off them: bookings,
    quotations (and their service/part line items, status logs, appliance
    links), invoices, payment transactions, cash-collection records,
    refunds, booking status logs, technician assignment history, GPS
    tracking points, SLA breaches, escalations, coupon usages, warranties +
    claims, technician ratings, customer appliances + their service
    history, AMC subscriptions + visits, CRM notes/follow-ups/tasks,
    in-app notifications, the linked User row, and (if present) the
    Firebase Auth account.

    THIS IS IRREVERSIBLE. There is no soft-delete/undo once this runs --
    use the regular DELETE (deactivate) endpoint instead if you just want
    to hide the customer while keeping their history intact.

    Financial/inventory ledger rows that exist independently of this
    customer's own record (technician commission payouts, the stock-
    movement ledger, technician stock logs, and direct-sale records) are
    intentionally NOT deleted, since deleting them would corrupt technician
    payroll history and inventory accounting that doesn't belong to the
    customer. Their booking_id reference is set to NULL instead so they
    remain valid but unlinked.
    """
    customer = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    from app.models.booking import Booking, BookingStatusLog
    from app.models.quotation import (
        Quotation, QuotationServiceItem, QuotationPartItem,
        QuotationStatusLog, QuotationAppliance,
    )
    from app.models.invoice import Invoice
    from app.models.payment import PaymentTransaction, CashCollectionRecord
    from app.models.refund import Refund
    from app.models.assignment import AssignmentHistory
    from app.models.tracking import TrackingLocation
    from app.models.sla import SLABreach
    from app.models.escalation import Escalation
    from app.models.commission import Commission
    from app.models.coupon import CouponUsage
    from app.models.warranty import Warranty, WarrantyClaim
    from app.models.technician import TechnicianRating
    from app.models.appliance import CustomerAppliance, ApplianceServiceHistory
    from app.models.inventory import StockMovement, TechnicianStockLog, DirectSale, BookingPartUsage
    from app.models.amc import AMCSubscription, AMCVisit
    from app.models.crm import CRMNote, CRMFollowup, CRMTask
    from app.models.notification import Notification

    booking_ids = (await db.execute(
        select(Booking.id).where(Booking.customer_id == customer_id)
    )).scalars().all()

    quotation_ids = []
    if booking_ids:
        quotation_ids = (await db.execute(
            select(Quotation.id).where(Quotation.booking_id.in_(booking_ids))
        )).scalars().all()

    warranty_ids = (await db.execute(
        select(Warranty.id).where(Warranty.customer_id == customer_id)
    )).scalars().all()

    amc_sub_ids = (await db.execute(
        select(AMCSubscription.id).where(AMCSubscription.customer_id == customer_id)
    )).scalars().all()

    appliance_ids = (await db.execute(
        select(CustomerAppliance.id).where(CustomerAppliance.customer_id == customer_id)
    )).scalars().all()

    user = (await db.execute(select(User).where(User.id == customer.user_id))).scalar_one_or_none()

    # ── Preserve ledger/payroll rows that don't belong to the customer ──────
    # Unlink instead of deleting so technician payouts & stock accounting
    # stay intact.
    if booking_ids:
        await db.execute(update(Commission).where(Commission.booking_id.in_(booking_ids)).values(booking_id=None))
        await db.execute(update(StockMovement).where(StockMovement.booking_id.in_(booking_ids)).values(booking_id=None))
        await db.execute(update(TechnicianStockLog).where(TechnicianStockLog.booking_id.in_(booking_ids)).values(booking_id=None))
        await db.execute(update(DirectSale).where(DirectSale.booking_id.in_(booking_ids)).values(booking_id=None))
    await db.execute(update(DirectSale).where(DirectSale.customer_id == customer_id).values(customer_id=None))

    # ── Quotation children + self-reference, then quotations themselves ────
    if quotation_ids:
        await db.execute(update(Quotation).where(Quotation.original_quotation_id.in_(quotation_ids)).values(original_quotation_id=None))
        await db.execute(delete(QuotationServiceItem).where(QuotationServiceItem.quotation_id.in_(quotation_ids)))
        await db.execute(delete(QuotationPartItem).where(QuotationPartItem.quotation_id.in_(quotation_ids)))
        await db.execute(delete(QuotationStatusLog).where(QuotationStatusLog.quotation_id.in_(quotation_ids)))
        await db.execute(delete(QuotationAppliance).where(QuotationAppliance.quotation_id.in_(quotation_ids)))

    # ── Payments / cash collection / refunds (must go before invoices) ─────
    if booking_ids:
        payment_ids = (await db.execute(
            select(PaymentTransaction.id).where(PaymentTransaction.booking_id.in_(booking_ids))
        )).scalars().all()
        if payment_ids:
            await db.execute(delete(CashCollectionRecord).where(CashCollectionRecord.payment_transaction_id.in_(payment_ids)))
            await db.execute(delete(Refund).where(Refund.payment_id.in_(payment_ids)))
        await db.execute(delete(Refund).where(Refund.booking_id.in_(booking_ids)))
        await db.execute(delete(PaymentTransaction).where(PaymentTransaction.booking_id.in_(booking_ids)))

    # ── Invoices (must go before quotations, since invoice.quotation_id is NOT NULL) ─
    if booking_ids:
        await db.execute(delete(Invoice).where(Invoice.booking_id.in_(booking_ids)))
    if quotation_ids:
        await db.execute(delete(Quotation).where(Quotation.id.in_(quotation_ids)))

    # ── Warranty claims, then warranties ────────────────────────────────────
    if warranty_ids:
        await db.execute(delete(WarrantyClaim).where(WarrantyClaim.warranty_id.in_(warranty_ids)))
    if booking_ids:
        await db.execute(delete(WarrantyClaim).where(WarrantyClaim.booking_id.in_(booking_ids)))
    await db.execute(delete(Warranty).where(Warranty.customer_id == customer_id))

    # ── AMC visits, then subscriptions ──────────────────────────────────────
    if amc_sub_ids:
        await db.execute(delete(AMCVisit).where(AMCVisit.amc_id.in_(amc_sub_ids)))
    await db.execute(delete(AMCSubscription).where(AMCSubscription.customer_id == customer_id))

    # ── Customer appliances + their service history ────────────────────────
    if appliance_ids:
        await db.execute(delete(ApplianceServiceHistory).where(ApplianceServiceHistory.appliance_id.in_(appliance_ids)))
    if booking_ids:
        await db.execute(delete(ApplianceServiceHistory).where(ApplianceServiceHistory.booking_id.in_(booking_ids)))
    await db.execute(delete(CustomerAppliance).where(CustomerAppliance.customer_id == customer_id))

    # ── Misc booking-linked records ─────────────────────────────────────────
    if booking_ids:
        await db.execute(delete(BookingStatusLog).where(BookingStatusLog.booking_id.in_(booking_ids)))
        await db.execute(delete(AssignmentHistory).where(AssignmentHistory.booking_id.in_(booking_ids)))
        await db.execute(delete(TrackingLocation).where(TrackingLocation.booking_id.in_(booking_ids)))
        await db.execute(delete(SLABreach).where(SLABreach.booking_id.in_(booking_ids)))
        await db.execute(delete(Escalation).where(Escalation.booking_id.in_(booking_ids)))
        await db.execute(delete(CouponUsage).where(CouponUsage.booking_id.in_(booking_ids)))
        await db.execute(delete(TechnicianRating).where(TechnicianRating.booking_id.in_(booking_ids)))
        await db.execute(delete(BookingPartUsage).where(BookingPartUsage.booking_id.in_(booking_ids)))

    await db.execute(delete(CouponUsage).where(CouponUsage.customer_id == customer_id))
    await db.execute(delete(TechnicianRating).where(TechnicianRating.customer_id == customer_id))

    # ── CRM records ──────────────────────────────────────────────────────────
    await db.execute(delete(CRMNote).where(CRMNote.customer_id == customer_id))
    await db.execute(delete(CRMFollowup).where(CRMFollowup.customer_id == customer_id))
    await db.execute(delete(CRMTask).where(CRMTask.customer_id == customer_id))

    # ── Bookings themselves ─────────────────────────────────────────────────
    if booking_ids:
        await db.execute(delete(Booking).where(Booking.id.in_(booking_ids)))

    # ── Customer addresses, notifications, customer, user ──────────────────
    await db.execute(delete(CustomerAddress).where(CustomerAddress.customer_id == customer_id))
    if user:
        await db.execute(delete(Notification).where(Notification.user_id == user.id))

    # Best-effort Firebase Auth cleanup -- don't block DB cleanup if this
    # fails (e.g. SDK not configured, UID already gone), but surface it.
    firebase_warning = None
    if user and user.firebase_uid:
        try:
            import asyncio
            from firebase_admin import auth as firebase_auth
            from app.utils.fcm import get_firebase_app
            fb_app = await get_firebase_app()
            if fb_app:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: firebase_auth.delete_user(user.firebase_uid, app=fb_app)
                )
            else:
                firebase_warning = "Firebase Admin SDK is not configured -- could not delete the Firebase Auth account automatically."
        except Exception as e:
            firebase_warning = f"Firebase Auth deletion failed ({e}) -- you may need to remove this user manually in the Firebase console."

    await db.delete(customer)
    if user:
        await db.delete(user)
    await db.commit()

    message = "Customer and all related records permanently deleted"
    if booking_ids:
        message += f" ({len(booking_ids)} booking(s) and their quotations/invoices/payments included)"
    if firebase_warning:
        message += f" ({firebase_warning})"
    return success_response(message=message)

@router.get("/{customer_id}/history", summary="Customer service history")
async def get_customer_history(customer_id: UUID, page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.booking import Booking
    bookings = (await db.execute(
        select(Booking).where(Booking.customer_id == customer_id)
        .order_by(Booking.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    )).scalars().all()
    return success_response(data={"items": [{"id": str(b.id), "booking_number": b.booking_number,
        "status": b.status.value, "scheduled_date": str(b.scheduled_date),
        "appliance_brand": b.appliance_brand, "appliance_model": b.appliance_model,
        "total_amount": b.total_amount} for b in bookings]})

@router.get("/{customer_id}/payments", summary="Customer payment history")
async def get_customer_payments(customer_id: UUID, page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.booking import Booking
    from app.models.payment import PaymentTransaction
    rows = (await db.execute(
        select(PaymentTransaction, Booking)
        .join(Booking, Booking.id == PaymentTransaction.booking_id)
        .where(Booking.customer_id == customer_id, PaymentTransaction.is_active == True)
        .order_by(PaymentTransaction.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    )).all()
    return success_response(data={"items": [{
        "payment_id": str(p.id), "transaction_number": p.transaction_number,
        "booking_number": b.booking_number, "method": p.method.value, "status": p.status.value,
        "amount": p.amount, "paid_at": p.paid_at.isoformat() if p.paid_at else None
    } for p, b in rows]})

@router.get("/{customer_id}/bookings", summary="Customer bookings")
async def get_customer_bookings(customer_id: UUID, page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.booking import Booking
    from app.models.service import Service
    # LEFT JOIN service + address to get names for both admin (FK) and public (free-text) bookings
    rows = (await db.execute(
        select(Booking, Service, CustomerAddress)
        .outerjoin(Service, Service.id == Booking.service_id)
        .outerjoin(CustomerAddress, CustomerAddress.id == Booking.address_id)
        .where(Booking.customer_id == customer_id)
        .order_by(Booking.created_at.desc())
        .offset((page-1)*per_page).limit(per_page)
    )).all()
    items = []
    for b, svc, addr in rows:
        svc_name = b.service_name or (svc.name if svc else None) or "—"
        if addr:
            addr_parts = [p for p in [addr.address_line1, addr.city, addr.pincode] if p]
            addr_str = ", ".join(addr_parts) if addr_parts else "—"
            addr_label = addr.label or ""
        else:
            addr_parts = [p for p in [b.address_line, b.city, b.pincode] if p]
            addr_str = ", ".join(addr_parts) if addr_parts else "—"
            addr_label = ""
        items.append({
            "id": str(b.id),
            "booking_number": b.booking_number,
            "status": b.status.value,
            "scheduled_date": str(b.scheduled_date),
            "total_amount": b.total_amount,
            "service_name": svc_name,
            "address_label": addr_label,
            "address_str": addr_str,
            "created_at": str(b.created_at) if b.created_at else None,
        })
    return success_response(data={"items": items})

# ── ADDRESSES ─────────────────────────────────────────────────
@router.get("/{customer_id}/addresses", summary="List customer addresses")
async def list_addresses(customer_id: UUID, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_or_404_owned_customer(customer_id, current_user, db)
    addresses = (await db.execute(
        select(CustomerAddress).where(CustomerAddress.customer_id == customer_id, CustomerAddress.is_active == True)
    )).scalars().all()
    return success_response(data=[{"id": str(a.id), "label": a.label, "address_line1": a.address_line1,
        "address_line2": a.address_line2, "city": a.city, "state": a.state,
        "pincode": a.pincode, "latitude": a.latitude, "longitude": a.longitude,
        "is_default": a.is_default} for a in addresses])

@router.post("/{customer_id}/addresses", summary="Add address")
async def add_address(customer_id: UUID, payload: CustomerAddressRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_or_404_owned_customer(customer_id, current_user, db)
    if payload.is_default:
        for addr in (await db.execute(select(CustomerAddress).where(CustomerAddress.customer_id == customer_id, CustomerAddress.is_default == True))).scalars().all():
            addr.is_default = False
    # Derive location_source if not provided
    loc_src = payload.location_source
    if not loc_src:
        if payload.latitude and payload.longitude:
            loc_src = "gps"
        else:
            loc_src = "manual"
    address = CustomerAddress(customer_id=customer_id, label=payload.label,
        address_line1=payload.address_line1, address_line2=payload.address_line2,
        city=payload.city, state=payload.state, pincode=payload.pincode,
        latitude=payload.latitude, longitude=payload.longitude,
        is_default=payload.is_default, location_source=loc_src)
    db.add(address)
    await db.flush()
    await db.commit()
    return success_response(data={"id": str(address.id)}, message="Address added successfully")

@router.put("/{customer_id}/addresses/{address_id}", summary="Update address")
async def update_address(customer_id: UUID, address_id: UUID, payload: CustomerAddressRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_or_404_owned_customer(customer_id, current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer_id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    loc_src = payload.location_source
    if not loc_src:
        if payload.latitude and payload.longitude:
            loc_src = "gps"
        else:
            loc_src = address.location_source or "manual"
    address.label = payload.label; address.address_line1 = payload.address_line1
    address.address_line2 = payload.address_line2; address.city = payload.city
    address.state = payload.state; address.pincode = payload.pincode
    address.latitude = payload.latitude; address.longitude = payload.longitude
    address.is_default = payload.is_default; address.location_source = loc_src
    await db.commit()
    return success_response(message="Address updated successfully")

@router.delete("/{customer_id}/addresses/{address_id}", summary="Delete address")
async def delete_address(customer_id: UUID, address_id: UUID, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_or_404_owned_customer(customer_id, current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer_id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    address.is_active = False
    await db.commit()  # BUG FIX: missing commit
    return success_response(message="Address deleted successfully")


# ── PATCH geo: save GPS coordinates to a customer address ─────────────────────
# Called by CCO portal (WhatsApp location paste) and Admin dashboard.
# Accepts raw lat/lng OR a WhatsApp/Google Maps share URL (parsed server-side).

class GeoUpdateRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    whatsapp_url: Optional[str] = None   # e.g. https://maps.google.com/?q=20.29,85.82
    location_source: str = "manual"       # 'gps'|'whatsapp'|'manual'|'geocoded'


def _extract_latlng_from_url(url: str):
    """
    Parse lat/lng from WhatsApp location share URLs and Google Maps URLs.
    Formats handled:
      https://maps.google.com/?q=20.2961,85.8245
      https://www.google.com/maps?q=20.2961,85.8245
      https://maps.google.com/maps?ll=20.2961,85.8245
      https://goo.gl/maps/...  (short — cannot resolve server-side, skip)
      https://maps.app.goo.gl/...  (short link — skip)
      https://www.google.com/maps/@20.2961,85.8245,17z
    """
    import re as _re
    patterns = [
        r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'/@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'loc:(-?\d+\.\d+),(-?\d+\.\d+)',
    ]
    for pat in patterns:
        m = _re.search(pat, url)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


@router.patch("/{customer_id}/addresses/{address_id}/geo",
              summary="Patch GPS coordinates on a customer address [CCO/Admin]")
async def patch_address_geo(
    customer_id: UUID,
    address_id: UUID,
    payload: GeoUpdateRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """
    CCO pastes a WhatsApp location share URL → server extracts lat/lng and
    saves to the address so the technician's EN_ROUTE map shows the correct
    destination.
    """
    await _get_or_404_owned_customer(customer_id, current_user, db)
    address = (await db.execute(
        select(CustomerAddress).where(
            CustomerAddress.id == address_id,
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.is_active == True,
        )
    )).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")

    lat, lng = payload.latitude, payload.longitude
    source = payload.location_source

    if payload.whatsapp_url:
        lat, lng = _extract_latlng_from_url(payload.whatsapp_url)
        if lat is None:
            raise HTTPException(
                status_code=422,
                detail="Could not extract coordinates from the provided URL. "
                       "Make sure it is a Google Maps link with lat/lng visible."
            )
        source = "whatsapp"

    if lat is None or lng is None:
        raise HTTPException(status_code=422, detail="Either lat/lng or a valid whatsapp_url is required")

    address.latitude = lat
    address.longitude = lng
    address.location_source = source
    await db.commit()

    return success_response(data={
        "address_id": str(address.id),
        "latitude": lat,
        "longitude": lng,
        "location_source": source,
    }, message="Location saved successfully")
