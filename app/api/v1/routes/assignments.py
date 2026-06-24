from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from app.core.database import get_db
from app.models.assignment import AssignmentHistory, AssignmentRule, AssignmentStatus, AssignmentType
from app.models.booking import Booking, BookingStatus, BookingStatusLog
from app.models.customer import CustomerAddress
from app.models.technician import Technician, TechnicianSkill, TechnicianStatus
from app.api.v1.schemas.assignment import (
    AutoAssignmentRequest,
    ManualAssignmentRequest,
    UpdateAssignmentRuleRequest,
)
from app.api.deps import AdminOrCCO
from app.utils.response import success_response

router = APIRouter()

ACTIVE_BOOKING_STATUSES = [
    BookingStatus.ASSIGNED,
    BookingStatus.ACCEPTED,
    BookingStatus.ARRIVED,
    BookingStatus.INSPECTING,
    BookingStatus.IN_PROGRESS,
]


async def _get_booking_or_404(db: AsyncSession, booking_id: UUID) -> Booking:
    booking = (await db.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


async def _get_default_rules(db: AsyncSession) -> AssignmentRule:
    rules = (await db.execute(select(AssignmentRule).where(AssignmentRule.name == "default"))).scalar_one_or_none()
    if not rules:
        rules = AssignmentRule(name="default")
        db.add(rules)
        await db.flush()
    return rules


async def _get_active_workload(db: AsyncSession, technician_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Booking.id)).where(
            Booking.technician_id == technician_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
    )
    return result.scalar_one() or 0


async def _add_booking_log(db: AsyncSession, booking: Booking, user_id: str, notes: str):
    db.add(
        BookingStatusLog(
            booking_id=booking.id,
            status=booking.status,
            changed_by=UUID(user_id),
            notes=notes,
        )
    )


async def _pick_best_technician(db: AsyncSession, booking: Booking, rules: AssignmentRule):
    technicians = (
        await db.execute(select(Technician).where(Technician.status == TechnicianStatus.ACTIVE))
    ).scalars().all()
    if not technicians:
        raise HTTPException(status_code=404, detail="No active technicians available")

    skill_rows = (
        await db.execute(select(TechnicianSkill).where(TechnicianSkill.service_id == booking.service_id))
    ).scalars().all()
    skill_match_ids = {row.technician_id for row in skill_rows}

    if rules.require_skill_match:
        technicians = [tech for tech in technicians if tech.id in skill_match_ids]
        if not technicians:
            raise HTTPException(status_code=404, detail="No technician found with required skill")

    address = (
        await db.execute(select(CustomerAddress).where(CustomerAddress.id == booking.address_id))
    ).scalar_one_or_none()
    if rules.prefer_same_city and address:
        same_city = [tech for tech in technicians if tech.city and tech.city.lower() == address.city.lower()]
        if same_city:
            technicians = same_city

    scored = []
    for tech in technicians:
        workload = await _get_active_workload(db, tech.id)
        if workload >= rules.max_active_bookings:
            continue
        score = 0.0
        if tech.id in skill_match_ids:
            score += 50
        if rules.prefer_high_rating:
            score += tech.rating * 20
        if rules.prefer_low_workload:
            score += max(0, 30 - workload * 10)
        score += max(0, 20 - tech.total_jobs * 0.1)
        scored.append((score, tech, workload))

    if not scored:
        raise HTTPException(status_code=404, detail="No technician available under current assignment rules")

    scored.sort(key=lambda item: (item[0], item[1].rating, -item[2]), reverse=True)
    return scored[0]


async def _apply_assignment(
    db: AsyncSession,
    booking: Booking,
    technician: Technician,
    assignment_type: AssignmentType,
    assigned_by: str,
    notes: str | None,
    score: float = 0.0,
):
    booking.technician_id = technician.id
    # Only move to ASSIGNED if booking is in an early/unstarted state.
    # For reassignment after work has progressed, preserve current status.
    _RESET_TO_ASSIGNED = {
        BookingStatus.PENDING, BookingStatus.CONFIRMED,
        BookingStatus.ASSIGNED, BookingStatus.ACCEPTED,
    }
    if booking.status in _RESET_TO_ASSIGNED:
        booking.status = BookingStatus.ASSIGNED
    # else: preserve current status (e.g. QUOTATION_APPROVED, IN_PROGRESS)
    db.add(
        AssignmentHistory(
            booking_id=booking.id,
            technician_id=technician.id,
            assigned_by=UUID(assigned_by),
            assignment_type=assignment_type,
            status=AssignmentStatus.ASSIGNED,
            score=score,
            notes=notes,
        )
    )
    await _add_booking_log(
        db,
        booking,
        assigned_by,
        notes or f"{assignment_type.value.title()} assignment completed",
    )
    await db.commit()


@router.post("/auto", summary="Auto assignment [Admin/CCO]")
async def auto_assign(
    payload: AutoAssignmentRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking_or_404(db, UUID(payload.booking_id))
    rules = await _get_default_rules(db)
    score, technician, workload = await _pick_best_technician(db, booking, rules)
    await _apply_assignment(
        db,
        booking,
        technician,
        AssignmentType.AUTO,
        current_user["user_id"],
        payload.notes,
        score,
    )
    return success_response(
        data={
            "booking_id": str(booking.id),
            "technician_id": str(technician.id),
            "technician_name": technician.name,
            "score": round(score, 2),
            "current_workload": workload,
        },
        message="Booking auto assigned successfully",
    )


@router.post("/manual", summary="Manual assignment [Admin/CCO]")
async def manual_assign(
    payload: ManualAssignmentRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking_or_404(db, UUID(payload.booking_id))
    technician = (
        await db.execute(select(Technician).where(Technician.id == UUID(payload.technician_id), Technician.status == TechnicianStatus.ACTIVE))
    ).scalar_one_or_none()
    if not technician:
        raise HTTPException(status_code=404, detail="Technician not found")
    await _apply_assignment(
        db,
        booking,
        technician,
        AssignmentType.MANUAL,
        current_user["user_id"],
        payload.notes,
    )
    return success_response(
        data={"booking_id": str(booking.id), "technician_id": str(technician.id)},
        message="Booking manually assigned successfully",
    )


@router.get("/history", summary="Assignment history [Admin/CCO]")
async def assignment_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    booking_id: str = Query(None),
    technician_id: str = Query(None),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    query = select(AssignmentHistory).order_by(AssignmentHistory.created_at.desc())
    if booking_id:
        query = query.where(AssignmentHistory.booking_id == UUID(booking_id))
    if technician_id:
        query = query.where(AssignmentHistory.technician_id == UUID(technician_id))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    items = (await db.execute(query.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return success_response(
        data={
            "items": [
                {
                    "id": str(item.id),
                    "booking_id": str(item.booking_id),
                    "technician_id": str(item.technician_id),
                    "assignment_type": item.assignment_type.value,
                    "status": item.status.value,
                    "score": item.score,
                    "notes": item.notes,
                    "created_at": item.created_at.isoformat(),
                }
                for item in items
            ],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
    )


@router.get("/rules", summary="Assignment rules [Admin/CCO]")
async def get_rules(
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    rules = await _get_default_rules(db)
    return success_response(
        data={
            "id": str(rules.id),
            "name": rules.name,
            "strategy": rules.strategy,
            "max_active_bookings": rules.max_active_bookings,
            "prefer_same_city": rules.prefer_same_city,
            "require_skill_match": rules.require_skill_match,
            "prefer_high_rating": rules.prefer_high_rating,
            "prefer_low_workload": rules.prefer_low_workload,
            "response_timeout_minutes": rules.response_timeout_minutes,
            "notes": rules.notes,
        }
    )


@router.put("/rules", summary="Update assignment rules [Admin/CCO]")
async def update_rules(
    payload: UpdateAssignmentRuleRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    rules = await _get_default_rules(db)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(rules, field, value)
    await db.commit()
    return success_response(message="Assignment rules updated successfully")
