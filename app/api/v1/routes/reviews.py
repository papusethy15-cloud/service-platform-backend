"""
Public reviews endpoint — reads from technician_ratings table.
When customer submits via /bookings/{id}/rate it saves to technician_ratings.
This endpoint surfaces those ratings with customer name/city from the booking.
"""
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.technician import TechnicianRating
from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer

router = APIRouter(tags=["reviews"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ReviewOut(BaseModel):
    id:            str
    customer_name: str
    customer_city: Optional[str] = None
    rating:        float
    review:        Optional[str] = None
    created_at:    Optional[str] = None

class ReviewStats(BaseModel):
    total:        int
    average:      float
    distribution: dict

class ReviewListResponse(BaseModel):
    stats:   ReviewStats
    reviews: List[ReviewOut]


# ── Helpers ────────────────────────────────────────────────────────────────────



# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.get("", response_model=ReviewListResponse)
async def list_reviews(
    domain_id:  Optional[str] = Query(None),
    limit:      int           = Query(20, ge=1, le=100),
    offset:     int           = Query(0, ge=0),
    min_rating: Optional[int] = Query(None, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    """
    Return customer reviews (from technician_ratings) for a domain.
    Joins booking → customer to get customer name and city.
    """
    # Base filter: rating must exist
    filters = [TechnicianRating.rating.isnot(None)]
    if min_rating:
        filters.append(TechnicianRating.rating >= min_rating)

    # If domain_id given, only include ratings whose booking belongs to that domain
    if domain_id:
        try:
            did = UUID(domain_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid domain_id")
        filters.append(
            TechnicianRating.booking_id.in_(
                select(Booking.id).where(Booking.domain_id == did)
            )
        )

    # ── Stats ──
    all_ratings_q = await db.execute(
        select(TechnicianRating.rating).where(and_(*filters))
    )
    all_ratings = all_ratings_q.scalars().all()
    total   = len(all_ratings)
    average = round(sum(all_ratings) / total, 1) if total else 0.0
    # distribution
    dist2: dict = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    for r in all_ratings:
        key = str(min(5, max(1, int(round(r)))))
        dist2[key] += 1

    # ── Rows (join booking + customer for name/city) ──
    stmt = (
        select(
            TechnicianRating.id,
            TechnicianRating.rating,
            TechnicianRating.review,
            TechnicianRating.created_at,
            Customer.name.label("customer_name"),
            Booking.city.label("customer_city"),
        )
        .outerjoin(Booking,  Booking.id  == TechnicianRating.booking_id)
        .outerjoin(Customer, Customer.id == TechnicianRating.customer_id)
        .where(and_(*filters))
        .order_by(TechnicianRating.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows_q = await db.execute(stmt)
    rows   = rows_q.all()

    reviews = [
        ReviewOut(
            id            = str(row.id),
            customer_name = row.customer_name or "Customer",
            customer_city = _extract_city(row.customer_city),
            rating        = row.rating,
            review        = row.review,
            created_at    = row.created_at.isoformat() if row.created_at else None,
        )
        for row in rows
    ]

    return ReviewListResponse(
        stats   = ReviewStats(total=total, average=average, distribution=dist2),
        reviews = reviews,
    )


def _extract_city(address: Optional[str]) -> Optional[str]:
    """Best-effort city extraction from address string."""
    if not address:
        return None
    # If it's a short string it's already a city
    if len(address) < 40 and "," not in address:
        return address
    # Take last meaningful part (city usually at end)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]  # city before state/pincode
    return parts[-1] if parts else None
