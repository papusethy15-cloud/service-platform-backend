from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from app.core.database import get_db
from app.models.customer import Customer, CustomerAddress
from app.models.user import User, UserRole
from app.api.v1.schemas.customer import (
    CreateCustomerRequest, UpdateCustomerRequest, CustomerAddressRequest
)
from app.api.deps import get_current_user, AdminOnly, AdminOrCCO
from app.utils.response import success_response
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


@router.get("/check-mobile/{mobile}", summary="Check if customer exists by mobile [Admin/CCO]")
async def check_customer_by_mobile(mobile: str, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
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
    address = CustomerAddress(customer_id=customer_id, label=payload.label,
        address_line1=payload.address_line1, address_line2=payload.address_line2,
        city=payload.city, state=payload.state, pincode=payload.pincode,
        latitude=payload.latitude, longitude=payload.longitude, is_default=payload.is_default)
    db.add(address)
    await db.flush()
    await db.commit()  # BUG FIX: missing commit
    return success_response(data={"id": str(address.id)}, message="Address added successfully")

@router.put("/{customer_id}/addresses/{address_id}", summary="Update address")
async def update_address(customer_id: UUID, address_id: UUID, payload: CustomerAddressRequest, current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_or_404_owned_customer(customer_id, current_user, db)
    address = (await db.execute(select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.customer_id == customer_id))).scalar_one_or_none()
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    address.label = payload.label; address.address_line1 = payload.address_line1
    address.address_line2 = payload.address_line2; address.city = payload.city
    address.state = payload.state; address.pincode = payload.pincode
    address.latitude = payload.latitude; address.longitude = payload.longitude
    address.is_default = payload.is_default
    await db.commit()  # BUG FIX: missing commit
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
