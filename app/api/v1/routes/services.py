"""
Services & Categories — Domain-Aware

Architecture:
  - service_categories and services are GLOBAL master records
  - domain_categories links a category to a domain
  - domain_services links a service to a domain
  - When admin creates a category/service with domain_id, the global record is created
    AND the domain link is saved in domain_categories / domain_services
  - GET /services?domain_id=X returns only services linked to that domain
  - GET /services/categories?domain_id=X returns only categories linked to that domain
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, exists
from uuid import UUID
from typing import Optional
from pydantic import BaseModel as PydanticBaseModel

from app.core.database import get_db
from app.models.service import Service, ServiceCategory
from app.models.domain import Domain, DomainCategory, DomainService, ServiceCityPrice
from app.models.city import City
from app.api.v1.schemas.service import (
    CreateServiceRequest, UpdateServiceRequest,
    CreateServiceCategoryRequest, UpdateServiceCategoryRequest
)
from app.api.deps import get_current_user, AdminOnly
from app.utils.response import success_response

router = APIRouter()

# ─── Inline schemas ───────────────────────────────────────────
class SetCityPriceRequest(PydanticBaseModel):
    city_id:      str
    price:        float
    is_available: bool = True

class UpdateCityPriceRequest(PydanticBaseModel):
    price:        Optional[float] = None
    is_available: Optional[bool]  = None

class CreateCategoryWithDomainRequest(PydanticBaseModel):
    name:        str
    description: Optional[str] = None
    icon:        Optional[str] = None
    sort_order:  int = 0
    domain_id:   Optional[str] = None   # if provided, auto-links to domain

class CreateServiceWithDomainRequest(PydanticBaseModel):
    category_id:  str
    name:         str
    description:  Optional[str] = None
    base_price:   float = 0.0
    gst_percent:  float = 18.0
    duration_mins: int = 60
    is_visible:   bool = True
    sort_order:   int = 0
    domain_id:    Optional[str] = None  # if provided, auto-links to domain

# ─── Helpers ──────────────────────────────────────────────────
async def _link_category_to_domain(db: AsyncSession, category_id: UUID, domain_id: UUID):
    existing = (await db.execute(
        select(DomainCategory).where(
            DomainCategory.domain_id   == domain_id,
            DomainCategory.category_id == category_id,
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(DomainCategory(domain_id=domain_id, category_id=category_id))

async def _link_service_to_domain(db: AsyncSession, service_id: UUID, domain_id: UUID):
    existing = (await db.execute(
        select(DomainService).where(
            DomainService.domain_id  == domain_id,
            DomainService.service_id == service_id,
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(DomainService(domain_id=domain_id, service_id=service_id))

def _category_row(c: ServiceCategory) -> dict:
    return {
        "id": str(c.id), "name": c.name, "description": c.description,
        "icon": c.icon, "sort_order": c.sort_order,
    }

def _service_row(s: Service) -> dict:
    return {
        "id": str(s.id), "category_id": str(s.category_id), "name": s.name,
        "description": s.description, "base_price": s.base_price,
        "gst_percent": s.gst_percent, "duration_mins": s.duration_mins,
        "is_visible": s.is_visible, "sort_order": s.sort_order,
    }

# ═══════════════════════════════════════════════════════════════
# ROUTE ORDER (FastAPI matches top-down — static segments first)
# 1. /city-prices/{price_id}         ← must come before /{service_id}
# 2. /categories                     ← static segment
# 3. /categories/{category_id}       ← after /categories
# 4. /                               ← list/create
# 5. /{service_id}/city-prices       ← sub-resource before /{service_id}
# 6. /{service_id}                   ← detail/update/delete
# ═══════════════════════════════════════════════════════════════

# ─── 1. City-price by record ID ───────────────────────────────
@router.put("/city-prices/{price_id}", summary="Update city price by ID [Admin]")
async def update_city_price(
    price_id: UUID,
    payload:  UpdateCityPriceRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    record = (await db.execute(
        select(ServiceCityPrice).where(ServiceCityPrice.id == price_id, ServiceCityPrice.is_active == True)
    )).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="City price record not found")
    if payload.price        is not None: record.price        = payload.price
    if payload.is_available is not None: record.is_available = payload.is_available
    await db.commit()
    return success_response(
        data={"id": str(record.id), "price": record.price, "is_available": record.is_available},
        message="City price updated"
    )

@router.delete("/city-prices/{price_id}", summary="Remove city price [Admin]")
async def delete_city_price(
    price_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    record = (await db.execute(
        select(ServiceCityPrice).where(ServiceCityPrice.id == price_id)
    )).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="City price record not found")
    record.is_active = False
    await db.commit()
    return success_response(message="City price removed")

# ─── 2-3. Categories ──────────────────────────────────────────
@router.get("/categories", summary="List service categories — filtered by domain_id [Public]")
async def list_categories(
    domain_id: Optional[str] = Query(None, description="Filter by domain UUID"),
    db: AsyncSession = Depends(get_db)
):
    """
    Without domain_id → returns ALL global categories (admin master list).
    With domain_id    → returns only categories linked to that domain.
    """
    if domain_id:
        # Join through domain_categories
        rows = (await db.execute(
            select(ServiceCategory)
            .join(DomainCategory, DomainCategory.category_id == ServiceCategory.id)
            .where(
                DomainCategory.domain_id == UUID(domain_id),
                DomainCategory.is_active == True,
                ServiceCategory.is_active == True,
            )
            .order_by(DomainCategory.sort_order, ServiceCategory.sort_order)
        )).scalars().all()
    else:
        rows = (await db.execute(
            select(ServiceCategory).where(ServiceCategory.is_active == True)
            .order_by(ServiceCategory.sort_order)
        )).scalars().all()

    return success_response(data=[_category_row(c) for c in rows])

@router.post("/categories", summary="Create service category [Admin] — optionally link to domain")
async def create_category(
    payload: CreateCategoryWithDomainRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    cat = ServiceCategory(
        name=payload.name, description=payload.description,
        icon=payload.icon or "", sort_order=payload.sort_order,
    )
    db.add(cat)
    await db.flush()

    if payload.domain_id:
        # Validate domain exists
        domain = (await db.execute(
            select(Domain).where(Domain.id == UUID(payload.domain_id), Domain.is_active == True)
        )).scalar_one_or_none()
        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found")
        await _link_category_to_domain(db, cat.id, UUID(payload.domain_id))

    await db.commit()
    return success_response(
        data={"id": str(cat.id), "name": cat.name, "domain_linked": bool(payload.domain_id)},
        message="Category created successfully"
    )

@router.put("/categories/{category_id}", summary="Update service category [Admin]")
async def update_category(
    category_id: UUID,
    payload: UpdateServiceCategoryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    cat = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id == category_id)
    )).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    for f, v in payload.model_dump(exclude_none=True).items():
        setattr(cat, f, v)
    await db.commit()
    return success_response(message="Category updated successfully")

@router.delete("/categories/{category_id}", summary="Delete service category [Admin]")
async def delete_category(
    category_id: UUID,
    domain_id: Optional[str] = Query(None, description="If given, removes only from this domain"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    if domain_id:
        # Remove domain link only — keep global record
        dc = (await db.execute(
            select(DomainCategory).where(
                DomainCategory.category_id == category_id,
                DomainCategory.domain_id   == UUID(domain_id),
            )
        )).scalar_one_or_none()
        if dc:
            dc.is_active = False
            await db.commit()
        return success_response(message="Category removed from domain")
    # Full soft-delete
    cat = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id == category_id)
    )).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    cat.is_active = False
    await db.commit()
    return success_response(message="Category deleted successfully")


# ─── 4a. Pending services (tech-suggested, awaiting admin verify) ─────────────
@router.get("/pending", summary="List tech-suggested services pending admin verification [Admin]")
async def list_pending_services(
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all Service records with is_pending_verify=1.
    Also fetches the QuotationServiceItem that triggered each suggestion
    so admin sees context (quotation, technician, price, appliance).
    """
    from app.models.quotation import QuotationServiceItem, Quotation
    from app.models.user import User

    pending_svcs = (await db.execute(
        select(Service).where(Service.is_pending_verify == 1, Service.is_active == False)
        .order_by(Service.created_at.desc())
    )).scalars().all()

    result = []
    for svc in pending_svcs:
        q_item = (await db.execute(
            select(QuotationServiceItem).where(
                QuotationServiceItem.service_id == svc.id,
                QuotationServiceItem.is_pending_verify == 1,
            ).order_by(QuotationServiceItem.created_at.desc()).limit(1)
        )).scalar_one_or_none()

        tech_name = None
        quotation_id = None
        booking_id = None
        if svc.suggested_by_tech:
            tech_user = (await db.execute(
                select(User).where(User.id == svc.suggested_by_tech)
            )).scalar_one_or_none()
            tech_name = tech_user.full_name if tech_user else None
        if q_item:
            quotation_id = str(q_item.quotation_id)
            from app.models.quotation import Quotation as Quot2
            quot = (await db.execute(
                select(Quot2).where(Quot2.id == q_item.quotation_id)
            )).scalar_one_or_none()
            if quot:
                booking_id = str(quot.booking_id)

        result.append({
            "service_id": str(svc.id),
            "name": svc.name,
            "base_price": svc.base_price,
            "gst_percent": svc.gst_percent,
            "duration_mins": svc.duration_mins,
            "description": svc.description,
            "created_at": svc.created_at.isoformat() if svc.created_at else None,
            "suggested_by_tech_id": str(svc.suggested_by_tech) if svc.suggested_by_tech else None,
            "suggested_by_tech_name": tech_name,
            "quotation_item_id": str(q_item.id) if q_item else None,
            "quotation_id": quotation_id,
            "booking_id": booking_id,
            "appliance_label": q_item.appliance_label if q_item else None,
            "unit_price": q_item.unit_price if q_item else svc.base_price,
        })

    return success_response(data={"items": result, "total": len(result)})


# ─── 4. Services list / create ────────────────────────────────
@router.get("", summary="List services — filtered by domain_id and/or category_id [Public]")
async def list_services(
    domain_id:    Optional[str]  = Query(None, description="Filter by domain UUID"),
    category_id:  Optional[UUID] = Query(None),
    visible_only: bool           = Query(True),
    per_page:     int            = Query(200),
    search:       Optional[str]  = Query(None, description="Search by name"),
    db: AsyncSession = Depends(get_db)
):
    """
    Without domain_id → returns ALL global services (admin master view).
    With domain_id    → returns only services linked to that domain.
    """
    if domain_id:
        q = (
            select(Service)
            .join(DomainService, DomainService.service_id == Service.id)
            .where(
                DomainService.domain_id == UUID(domain_id),
                DomainService.is_active == True,
                Service.is_active       == True,
            )
        )
    else:
        q = select(Service).where(Service.is_active == True)

    if category_id:   q = q.where(Service.category_id == category_id)
    if visible_only:  q = q.where(Service.is_visible   == True)
    if search:        q = q.where(Service.name.ilike(f"%{search}%"))
    q = q.order_by(Service.sort_order).limit(per_page)

    services = (await db.execute(q)).scalars().all()
    return success_response(data={"items": [_service_row(s) for s in services], "total": len(services)})

@router.post("", summary="Create service [Admin] — optionally link to domain")
async def create_service(
    payload: CreateServiceWithDomainRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    # Validate category
    cat = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id == UUID(payload.category_id))
    )).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    service = Service(
        category_id   = UUID(payload.category_id),
        name          = payload.name,
        description   = payload.description,
        base_price    = payload.base_price,
        gst_percent   = payload.gst_percent,
        duration_mins = payload.duration_mins,
        is_visible    = payload.is_visible,
        sort_order    = payload.sort_order,
    )
    db.add(service)
    await db.flush()

    if payload.domain_id:
        domain = (await db.execute(
            select(Domain).where(Domain.id == UUID(payload.domain_id), Domain.is_active == True)
        )).scalar_one_or_none()
        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found")
        # Auto-link service to domain
        await _link_service_to_domain(db, service.id, UUID(payload.domain_id))
        # Also auto-link the category to the domain (so category filter works)
        await _link_category_to_domain(db, UUID(payload.category_id), UUID(payload.domain_id))

    await db.commit()
    return success_response(
        data={"id": str(service.id), "name": service.name, "base_price": service.base_price,
              "domain_linked": bool(payload.domain_id)},
        message="Service created successfully"
    )

# ─── 5. City prices by service ────────────────────────────────
@router.get("/{service_id}/city-prices", summary="Get city-wise prices for a service [Public]")
async def get_city_prices(service_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = (await db.execute(
        select(Service).where(Service.id == service_id, Service.is_active == True)
    )).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")

    rows = (await db.execute(
        select(ServiceCityPrice, City.name.label("city_name"), City.state.label("city_state"))
        .join(City, City.id == ServiceCityPrice.city_id)
        .where(ServiceCityPrice.service_id == service_id, ServiceCityPrice.is_active == True)
        .order_by(City.name)
    )).all()

    return success_response(data=[{
        "id":           str(r.ServiceCityPrice.id),
        "service_id":   str(r.ServiceCityPrice.service_id),
        "city_id":      str(r.ServiceCityPrice.city_id),
        "city_name":    r.city_name,
        "city_state":   r.city_state,
        "price":        r.ServiceCityPrice.price,
        "is_available": r.ServiceCityPrice.is_available,
    } for r in rows])

@router.post("/{service_id}/city-prices", summary="Set / upsert city price [Admin]")
async def set_city_price(
    service_id: UUID,
    payload:    SetCityPriceRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    svc = (await db.execute(
        select(Service).where(Service.id == service_id, Service.is_active == True)
    )).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")

    city = (await db.execute(
        select(City).where(City.id == UUID(payload.city_id), City.is_active == True)
    )).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    existing = (await db.execute(
        select(ServiceCityPrice).where(
            ServiceCityPrice.service_id == service_id,
            ServiceCityPrice.city_id   == UUID(payload.city_id),
        )
    )).scalar_one_or_none()

    if existing:
        existing.price = payload.price; existing.is_available = payload.is_available; existing.is_active = True
        record = existing
        msg = "City price updated"
    else:
        record = ServiceCityPrice(service_id=service_id, city_id=UUID(payload.city_id),
                                   price=payload.price, is_available=payload.is_available)
        db.add(record)
        msg = "City price set"

    await db.commit()
    return success_response(data={
        "id": str(record.id), "service_id": str(service_id),
        "city_id": str(record.city_id), "city_name": city.name,
        "city_state": city.state, "price": record.price, "is_available": record.is_available,
    }, message=msg)

# ─── 6. Service detail / update / delete ──────────────────────
@router.get("/{service_id}", summary="Get service details [Public]")
async def get_service(service_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = (await db.execute(
        select(Service).where(Service.id == service_id, Service.is_active == True)
    )).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return success_response(data=_service_row(svc))

@router.put("/{service_id}", summary="Update service [Admin]")
async def update_service(
    service_id: UUID,
    payload: UpdateServiceRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    svc = (await db.execute(select(Service).where(Service.id == service_id))).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    for f, v in payload.model_dump(exclude_none=True).items():
        setattr(svc, f, v)
    await db.commit()
    return success_response(message="Service updated successfully")

@router.delete("/{service_id}", summary="Delete service [Admin]")
async def delete_service(
    service_id: UUID,
    domain_id: Optional[str] = Query(None, description="If given, removes only from this domain"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    if domain_id:
        # Remove domain link only
        ds = (await db.execute(
            select(DomainService).where(
                DomainService.service_id == service_id,
                DomainService.domain_id  == UUID(domain_id),
            )
        )).scalar_one_or_none()
        if ds:
            ds.is_active = False
            await db.commit()
        return success_response(message="Service removed from domain")
    # Full soft-delete
    svc = (await db.execute(select(Service).where(Service.id == service_id))).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    svc.is_active = False
    await db.commit()
    return success_response(message="Service deleted successfully")
