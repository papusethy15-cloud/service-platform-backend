from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from uuid import UUID
import random, string

from app.core.database import get_db
from app.models.technician import Technician, TechnicianSkill, TechnicianAvailability, TechnicianStatus, TechnicianRating
from app.models.user import User, UserRole
from app.core.security import hash_password
from app.api.v1.schemas.technician import (
    CreateTechnicianRequest, UpdateTechnicianRequest, AddTechnicianSkillRequest
)
from app.api.deps import get_current_user, AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response, iso

router = APIRouter()


def generate_tech_code():
    return "TECH" + ''.join(random.choices(string.digits, k=5))


# ── PUBLIC: Online technician locations (for customer map view) ──────────────
@router.get("/live-locations", summary="Online technician locations [Customer]")
async def live_technician_locations(
    city: str = Query(None, description="Filter by city name (optional)"),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """Returns id, name, city, last_lat, last_lng for all ONLINE technicians.
    Only exposes fields safe for customer display — no mobile / identity data.
    """
    q = select(Technician).where(Technician.is_online.is_(True))
    if city:
        q = q.where(Technician.city.ilike(f"%{city}%"))
    techs = (await db.execute(q.limit(200))).scalars().all()
    return success_response(data=[
        {
            "id": str(t.id),
            "name": t.name,
            "city": t.city or "",
            "area": t.area or "",
            "latitude": float(t.last_lat) if t.last_lat else None,
            "longitude": float(t.last_lng) if t.last_lng else None,
            "rating": round(float(t.rating), 1) if t.rating else None,
        }
        for t in techs
    ])


# ── PUBLIC: Recent customer reviews (for certified-technicians screen) ────────
@router.get("/public-reviews", summary="Recent customer reviews [Customer]")
async def public_reviews(
    limit: int = Query(10, ge=1, le=50, description="Number of reviews to return"),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    """Returns up to `limit` random reviews with a non-empty review text and
    rating >= 4.  Safe for public display — customer name is first name only.
    """
    from app.models.booking import Booking
    from app.models.customer import Customer
    from sqlalchemy import text as _text
    # Join ratings → bookings → customers to get reviewer first name + service name
    rows = (await db.execute(
        select(
            TechnicianRating.rating,
            TechnicianRating.review,
            TechnicianRating.created_at,
            Customer.name.label("customer_name"),
            Booking.service_name,
        )
        .join(Booking, TechnicianRating.booking_id == Booking.id, isouter=True)
        .join(Customer, TechnicianRating.customer_id == Customer.id, isouter=True)
        .where(
            TechnicianRating.review.isnot(None),
            TechnicianRating.review != "",
            TechnicianRating.rating >= 4,
        )
        .order_by(func.random())
        .limit(limit)
    )).all()

    return success_response(data=[
        {
            "rating": round(float(r.rating), 1),
            "review": r.review,
            "customer_name": (r.customer_name or "Customer").split()[0],  # first name only
            "service_name": r.service_name or "",
            "created_at": iso(r.created_at) if r.created_at else None,
        }
        for r in rows
    ])


# ── LIST ───────────────────────────────────────────────────────────────────────
@router.get("", summary="List technicians [Admin/CCO]")
async def list_technicians(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=1000),
    city: str = Query(None),
    status: str = Query(None),
    search: str = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    q = select(Technician)
    if city:   q = q.where(Technician.city == city)
    if status:
        try:
            q = q.where(Technician.status == TechnicianStatus(status))
        except ValueError:
            pass
    if search:
        like = f"%{search}%"
        q = q.where(or_(
            Technician.name.ilike(like),
            Technician.mobile.ilike(like),
            Technician.technician_code.ilike(like),
            Technician.email.ilike(like),
        ))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    techs = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()

    return success_response(data={
        "technicians": [_serialize(t) for t in techs],
        "total": total, "page": page, "per_page": per_page,
        "pages": -(-total // per_page),
    })


# ── CREATE (with optional skills + availability) ───────────────────────────────
@router.post("", summary="Create technician [Admin]")
async def create_technician(
    payload: CreateTechnicianRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy.exc import IntegrityError

    # ── Pre-flight: check for duplicate mobile / email in users table ──────────
    existing_mobile = (await db.execute(
        select(User).where(User.mobile == payload.mobile)
    )).scalar_one_or_none()
    if existing_mobile:
        role_label = existing_mobile.role.value.lower().replace("_", " ")
        raise HTTPException(
            status_code=400,
            detail=f"Mobile number {payload.mobile} is already registered as a {role_label}. "
                   "Mobile numbers must be unique across customers, technicians, and staff."
        )

    if payload.email:
        existing_email = (await db.execute(
            select(User).where(User.email == payload.email)
        )).scalar_one_or_none()
        if existing_email:
            raise HTTPException(
                status_code=400,
                detail=f"A user with email {payload.email} already exists. "
                       "Use a different email or leave it blank."
            )

    try:
        # Create auth user
        user = User(
            name=payload.name,
            mobile=payload.mobile,
            email=payload.email,
            role=UserRole.TECHNICIAN,
            is_verified=True,
            password_hash=hash_password(payload.mobile),  # default pwd = mobile
        )
        db.add(user)
        await db.flush()

        # Create technician profile
        tech = Technician(
            user_id=user.id,
            name=payload.name,
            mobile=payload.mobile,
            email=payload.email,
            alternate_mobile=payload.alternate_mobile,
            city=payload.city,
            area=payload.area,
            address=payload.address,
            pincode=payload.pincode,
            experience_years=payload.experience_years,
            dob=payload.dob,
            gender=payload.gender,
            emergency_contact_name=payload.emergency_contact_name,
            emergency_contact_mobile=payload.emergency_contact_mobile,
            identity_type=payload.identity_type,
            identity_number=payload.identity_number,
            technician_code=generate_tech_code(),
            # Payout / payment details
            payout_upi_id=payload.payout_upi_id,
            payout_bank_account=payload.payout_bank_account,
            payout_bank_ifsc=payload.payout_bank_ifsc,
            payout_bank_name=payload.payout_bank_name,
            payout_account_holder=payload.payout_account_holder,
        )
        db.add(tech)
        await db.flush()

        # Add skills if provided
        if payload.skills:
            for sk in payload.skills:
                if sk.get("service_id"):
                    skill = TechnicianSkill(
                        technician_id=tech.id,
                        service_id=UUID(sk["service_id"]),
                        proficiency=sk.get("proficiency", "INTERMEDIATE"),
                    )
                    db.add(skill)

        # Add availability if provided
        if payload.availability:
            for slot in payload.availability:
                avail = TechnicianAvailability(
                    technician_id=tech.id,
                    day_of_week=slot.get("day_of_week", 0),
                    start_time=slot.get("start_time", "09:00:00"),
                    end_time=slot.get("end_time", "18:00:00"),
                    is_available=slot.get("is_available", True),
                )
                db.add(avail)

        await db.commit()
        return success_response(
            data={"id": str(tech.id), "technician_code": tech.technician_code},
            message="Technician registered successfully"
        )

    except IntegrityError as exc:
        await db.rollback()
        detail = str(exc.orig) if exc.orig else str(exc)
        if "users_mobile_key" in detail or "mobile" in detail:
            raise HTTPException(
                status_code=400,
                detail=f"Mobile number {payload.mobile} is already registered. "
                       "Each technician must have a unique mobile number."
            )
        if "users_email_key" in detail or "email" in detail:
            raise HTTPException(
                status_code=400,
                detail=f"Email {payload.email} is already registered. "
                       "Use a different email or leave it blank."
            )
        # Re-raise any other integrity errors as 400 (not 500)
        raise HTTPException(status_code=400, detail=f"Database constraint error: {detail}")


# ── GET ────────────────────────────────────────────────────────────────────────
@router.get("/{tech_id}", summary="Technician details")
async def get_technician(
    tech_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    tech = await _get_or_404(tech_id, db)
    return success_response(data=_serialize(tech, full=True))


# ── UPDATE ─────────────────────────────────────────────────────────────────────
@router.put("/{tech_id}", summary="Update technician [Admin]")
async def update_technician(
    tech_id: UUID,
    payload: UpdateTechnicianRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    tech = await _get_or_404(tech_id, db)
    fields = payload.model_dump(exclude_unset=True)
    for field, val in fields.items():
        if field == "status":
            try:
                setattr(tech, "status", TechnicianStatus(val))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {val}")
        else:
            setattr(tech, field, val)
    await db.commit()
    return success_response(message="Technician updated")


# ── DEACTIVATE ─────────────────────────────────────────────────────────────────
@router.delete("/{tech_id}", summary="Deactivate technician [Admin]")
async def deactivate_technician(
    tech_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    tech = await _get_or_404(tech_id, db)
    tech.status = TechnicianStatus.INACTIVE
    await db.commit()
    return success_response(message="Technician deactivated")


# ── SKILLS ─────────────────────────────────────────────────────────────────────
@router.get("/{tech_id}/skills", summary="List technician skills")
async def get_skills(
    tech_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    skills = (await db.execute(
        select(TechnicianSkill).where(TechnicianSkill.technician_id == tech_id)
    )).scalars().all()
    return success_response(data=[{
        "id": str(s.id),
        "service_id": str(s.service_id),
        "proficiency": s.proficiency,
    } for s in skills])


@router.post("/{tech_id}/skills", summary="Add skill [Admin]")
async def add_skill(
    tech_id: UUID,
    payload: AddTechnicianSkillRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    # Prevent duplicate skill for same service
    existing = (await db.execute(
        select(TechnicianSkill).where(
            TechnicianSkill.technician_id == tech_id,
            TechnicianSkill.service_id == UUID(payload.service_id)
        )
    )).scalar_one_or_none()

    if existing:
        existing.proficiency = payload.proficiency
    else:
        db.add(TechnicianSkill(
            technician_id=tech_id,
            service_id=UUID(payload.service_id),
            proficiency=payload.proficiency
        ))
    await db.commit()
    return success_response(message="Skill saved")


@router.delete("/{tech_id}/skills/{skill_id}", summary="Remove skill [Admin]")
async def remove_skill(
    tech_id: UUID,
    skill_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    skill = (await db.execute(
        select(TechnicianSkill).where(
            TechnicianSkill.id == skill_id,
            TechnicianSkill.technician_id == tech_id,
        )
    )).scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    await db.delete(skill)
    await db.commit()
    return success_response(message="Skill removed")


# ── AVAILABILITY ───────────────────────────────────────────────────────────────
@router.get("/{tech_id}/availability", summary="Technician availability schedule")
async def get_availability(
    tech_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    slots = (await db.execute(
        select(TechnicianAvailability).where(TechnicianAvailability.technician_id == tech_id)
    )).scalars().all()
    return success_response(data=[{
        "id": str(s.id),
        "day_of_week": s.day_of_week,
        "start_time": s.start_time,
        "end_time": s.end_time,
        "is_available": s.is_available,
    } for s in slots])


@router.put("/{tech_id}/availability", summary="Set full availability schedule [Admin]")
async def set_availability(
    tech_id: UUID,
    slots: list,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    """Replace entire availability schedule."""
    await db.execute(
        TechnicianAvailability.__table__.delete().where(
            TechnicianAvailability.technician_id == tech_id
        )
    )
    for slot in slots:
        db.add(TechnicianAvailability(
            technician_id=tech_id,
            day_of_week=slot.get("day_of_week", 0),
            start_time=slot.get("start_time", "09:00:00"),
            end_time=slot.get("end_time", "18:00:00"),
            is_available=slot.get("is_available", True),
        ))
    await db.commit()
    return success_response(message="Availability updated")


# ── RATINGS / PERFORMANCE ──────────────────────────────────────────────────────
@router.get("/{tech_id}/ratings", summary="Technician ratings")
async def get_ratings(
    tech_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    tech = await _get_or_404(tech_id, db)
    return success_response(data={
        "technician_id": str(tech_id),
        "rating": tech.rating,
        "total_jobs": tech.total_jobs
    })


@router.get("/{tech_id}/performance", summary="Technician performance metrics")
async def get_performance(
    tech_id: UUID,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db)
):
    from app.models.booking import Booking, BookingStatus
    total = (await db.execute(
        select(func.count(Booking.id)).where(Booking.technician_id == tech_id)
    )).scalar_one()
    completed = (await db.execute(
        select(func.count(Booking.id)).where(
            Booking.technician_id == tech_id,
            Booking.status == BookingStatus.COMPLETED
        )
    )).scalar_one()
    return success_response(data={
        "technician_id": str(tech_id),
        "total_assigned": total,
        "completed": completed,
        "completion_rate": round((completed / total * 100) if total else 0, 2),
    })


# ── Helpers ────────────────────────────────────────────────────────────────────
async def _get_or_404(tech_id: UUID, db: AsyncSession) -> Technician:
    result = await db.execute(select(Technician).where(Technician.id == tech_id))
    tech = result.scalar_one_or_none()
    if not tech:
        raise HTTPException(status_code=404, detail="Technician not found")
    return tech


def _serialize(t: Technician, full: bool = False) -> dict:
    base = {
        "id": str(t.id),
        "name": t.name,
        "mobile": t.mobile,
        "email": t.email,
        "technician_code": t.technician_code,
        "city": t.city,
        "area": t.area,
        "status": t.status.value,
        "experience_years": t.experience_years,
        "rating": t.rating,
        "total_jobs": t.total_jobs,
        "is_online": bool(getattr(t, "is_online", False)),
        "auto_assign_eligible": bool(getattr(t, "auto_assign_eligible", True)),
    }
    if full:
        base.update({
            "alternate_mobile": t.alternate_mobile,
            "dob": iso(t.dob) if t.dob else None,
            "gender": t.gender,
            "pincode": t.pincode,
            "address": t.address,
            "profile_image": t.profile_image,
            "identity_type": t.identity_type,
            "identity_number": t.identity_number,
            "id_proof": t.id_proof,
            "emergency_contact_name": t.emergency_contact_name,
            "emergency_contact_mobile": t.emergency_contact_mobile,
            "payout_upi_id":          t.payout_upi_id,
            "payout_bank_account":    t.payout_bank_account,
            "payout_bank_ifsc":       t.payout_bank_ifsc,
            "payout_bank_name":       t.payout_bank_name,
            "payout_account_holder":  t.payout_account_holder,
            "payout_method_verified": t.payout_method_verified if t.payout_method_verified is not None else False,
            "has_payout_method":      bool(t.payout_upi_id or t.payout_bank_account),
            "created_at": iso(t.created_at) if hasattr(t, 'created_at') and t.created_at else None,
        })
    return base


# ── PROFILE IMAGE ─────────────────────────────────────────────────────────────
class UpdateProfileImageRequest(BaseModel):
    profile_image: str

@router.put("/{tech_id}/profile-image", summary="Update technician profile image [Admin]")
async def update_profile_image(
    tech_id: UUID,
    payload: UpdateProfileImageRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from pydantic import BaseModel as _BM
    tech = await _get_or_404(tech_id, db)
    tech.profile_image = payload.profile_image
    await db.commit()
    return success_response(data={"profile_image": tech.profile_image}, message="Profile image updated")


# ── ID PROOF ──────────────────────────────────────────────────────────────────
class UpdateDocRequest(BaseModel):
    id_proof:        str | None = None
    identity_type:   str | None = None
    identity_number: str | None = None

@router.put("/{tech_id}/documents", summary="Update technician documents [Admin]")
async def update_documents(
    tech_id: UUID,
    payload: UpdateDocRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    tech = await _get_or_404(tech_id, db)
    if payload.id_proof        is not None: tech.id_proof        = payload.id_proof
    if payload.identity_type   is not None: tech.identity_type   = payload.identity_type
    if payload.identity_number is not None: tech.identity_number = payload.identity_number
    await db.commit()
    return success_response(message="Documents updated")
