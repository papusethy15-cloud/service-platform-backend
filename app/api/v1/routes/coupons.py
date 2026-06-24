from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

# Normalise frontend values to DB canonical values
DISCOUNT_TYPE_MAP = {"PERCENT": "PERCENTAGE", "PERCENTAGE": "PERCENTAGE", "FIXED": "FLAT", "FLAT": "FLAT"}


class CreateCouponRequest(BaseModel):
    code: str
    description: Optional[str] = None
    discount_type: str              # PERCENT/PERCENTAGE/FIXED/FLAT accepted
    discount_value: float
    min_order_amount: float = 0.0
    max_discount_amount: Optional[float] = None
    usage_limit: Optional[int] = None
    max_uses: Optional[int] = None  # alias accepted from frontend
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    domain_id: Optional[str] = None  # NULL = global coupon (valid on all domains)


class ValidateCouponRequest(BaseModel):
    code: str
    order_amount: float
    domain_id: Optional[str] = None  # website sends its own domain_id for scoped validation


def _coupon_dict(c, domain_name: str = None) -> dict:
    return {
        "id": str(c.id),
        "code": c.code,
        "description": c.description,
        "discount_type": c.discount_type,
        "discount_value": c.discount_value,
        "min_order_amount": c.min_order_amount or 0,
        "max_discount_amount": c.max_discount_amount,
        "usage_limit": c.usage_limit,
        "used_count": c.used_count or 0,
        "valid_from": c.valid_from.isoformat() if c.valid_from else None,
        "valid_until": c.valid_until.isoformat() if c.valid_until else None,
        "is_active": c.is_active,
        "domain_id": str(c.domain_id) if c.domain_id else None,
        "domain_name": domain_name,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("", summary="List coupons [Admin]")
async def list_coupons(
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    domain_id: Optional[str] = Query(None, description="Filter by domain (omit for all)"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.coupon import Coupon
    from app.models.domain import Domain

    q = select(Coupon)
    if domain_id:
        # Show coupons scoped to that domain OR global coupons (domain_id IS NULL)
        q = q.where(or_(Coupon.domain_id == domain_id, Coupon.domain_id.is_(None)))
    q = q.order_by(Coupon.created_at.desc())

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    pages = max(1, -(-total // per_page))
    coupons = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()

    # Fetch domain names for display
    domain_ids = list({str(c.domain_id) for c in coupons if c.domain_id})
    domain_map: dict = {}
    if domain_ids:
        rows = (await db.execute(select(Domain).where(Domain.id.in_(domain_ids)))).scalars().all()
        domain_map = {str(r.id): r.name for r in rows}

    return success_response(data={
        "items": [_coupon_dict(c, domain_map.get(str(c.domain_id))) for c in coupons],
        "total": total,
        "page": page,
        "pages": pages,
    })


@router.post("", summary="Create coupon [Admin]")
async def create_coupon(
    payload: CreateCouponRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.coupon import Coupon
    dt = DISCOUNT_TYPE_MAP.get(payload.discount_type.upper(), "PERCENTAGE")
    usage_limit = payload.usage_limit or payload.max_uses or None
    domain_id = UUID(payload.domain_id) if payload.domain_id else None

    # Check duplicate: same code + same domain scope
    q = select(Coupon).where(Coupon.code == payload.code.upper())
    if domain_id:
        q = q.where(Coupon.domain_id == domain_id)
    else:
        q = q.where(Coupon.domain_id.is_(None))
    existing = (await db.execute(q)).scalar_one_or_none()
    if existing:
        scope = f"domain '{payload.domain_id}'" if domain_id else "global scope"
        raise HTTPException(400, f"Coupon code '{payload.code.upper()}' already exists in {scope}")

    coupon = Coupon(
        code=payload.code.upper(),
        description=payload.description,
        discount_type=dt,
        discount_value=payload.discount_value,
        min_order_amount=payload.min_order_amount,
        max_discount_amount=payload.max_discount_amount,
        usage_limit=usage_limit,
        used_count=0,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        is_active=True,
        domain_id=domain_id,
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    return success_response(data=_coupon_dict(coupon), message="Coupon created")


@router.post("/validate", summary="Validate a coupon code")
async def validate_coupon(
    payload: ValidateCouponRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.coupon import Coupon
    from datetime import timezone

    # Match coupon: code + (domain-scoped OR global)
    # Priority: domain-specific coupon first, then global fallback
    coupon = None
    if payload.domain_id:
        domain_uuid = UUID(payload.domain_id)
        # Try domain-specific first
        coupon = (await db.execute(
            select(Coupon).where(
                Coupon.code == payload.code.upper(),
                Coupon.is_active == True,
                Coupon.domain_id == domain_uuid,
            )
        )).scalar_one_or_none()
        # Fallback to global coupon
        if not coupon:
            coupon = (await db.execute(
                select(Coupon).where(
                    Coupon.code == payload.code.upper(),
                    Coupon.is_active == True,
                    Coupon.domain_id.is_(None),
                )
            )).scalar_one_or_none()
    else:
        # No domain_id provided — only match global coupons
        coupon = (await db.execute(
            select(Coupon).where(
                Coupon.code == payload.code.upper(),
                Coupon.is_active == True,
                Coupon.domain_id.is_(None),
            )
        )).scalar_one_or_none()

    if not coupon:
        raise HTTPException(404, "Invalid coupon code")

    now = datetime.now(timezone.utc)
    if coupon.valid_until and coupon.valid_until.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(400, "Coupon has expired")
    if coupon.valid_from and coupon.valid_from.replace(tzinfo=timezone.utc) > now:
        raise HTTPException(400, "Coupon is not yet valid")
    if coupon.usage_limit and (coupon.used_count or 0) >= coupon.usage_limit:
        raise HTTPException(400, "Coupon usage limit reached")
    if payload.order_amount < (coupon.min_order_amount or 0):
        raise HTTPException(400, f"Minimum order amount is \u20b9{coupon.min_order_amount:.0f}")

    if coupon.discount_type == "FLAT":
        discount = coupon.discount_value
    else:
        discount = payload.order_amount * coupon.discount_value / 100
    if coupon.max_discount_amount:
        discount = min(discount, coupon.max_discount_amount)
    discount = round(discount, 2)

    return success_response(data={
        "coupon_id": str(coupon.id),
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value,
        "discount_amount": discount,
        "description": coupon.description,
        "is_global": coupon.domain_id is None,
    }, message="Coupon valid")


@router.put("/{coupon_id}", summary="Update coupon [Admin]")
async def update_coupon(
    coupon_id: UUID,
    payload: CreateCouponRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.coupon import Coupon
    c = (await db.execute(select(Coupon).where(Coupon.id == coupon_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Coupon not found")
    dt = DISCOUNT_TYPE_MAP.get(payload.discount_type.upper(), "PERCENTAGE")
    c.code = payload.code.upper()
    c.description = payload.description
    c.discount_type = dt
    c.discount_value = payload.discount_value
    c.min_order_amount = payload.min_order_amount
    c.max_discount_amount = payload.max_discount_amount
    c.usage_limit = payload.usage_limit or payload.max_uses or None
    c.valid_from = payload.valid_from
    c.valid_until = payload.valid_until
    c.domain_id = UUID(payload.domain_id) if payload.domain_id else None
    await db.commit()
    await db.refresh(c)
    return success_response(data=_coupon_dict(c), message="Coupon updated")


@router.delete("/{coupon_id}", summary="Deactivate coupon [Admin]")
async def deactivate_coupon(
    coupon_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.coupon import Coupon
    c = (await db.execute(select(Coupon).where(Coupon.id == coupon_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Coupon not found")
    c.is_active = False
    await db.commit()
    return success_response(message="Coupon deactivated")
