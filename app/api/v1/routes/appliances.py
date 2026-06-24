"""
Appliances Module  —  /api/v1/appliances

Architecture:
  - ApplianceBrand      → brand (LG, Samsung, Daikin…)
  - ApplianceType       → type/variant under brand+category (1.5T Split AC…)
  - appliance_category_id → FK to service_categories  [UNIFIED CATEGORY]
  - CustomerAppliance   → customer's physical unit
  - ApplianceServiceHistory → completed service records

Domain filtering chain:
  Domain → DomainCategory (service_categories) → ApplianceType.appliance_category_id
  → brands/types available for that domain's categories

Public endpoints (no auth) for customer app/website:
  GET /appliances/categories        → list service_categories (with appliance flag)
  GET /appliances/brands            → brands, filterable by appliance_category_id
  GET /appliances/types             → types, filterable by brand_id / appliance_category_id
  GET /appliances/domain/{slug}     → full catalogue for a domain (categories→brands→types)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from uuid import UUID
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel

from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

# ── Pydantic schemas ───────────────────────────────────────────────────────

class CreateBrandRequest(BaseModel):
    name:             str
    logo_url:         Optional[str]       = None
    category_ids:     List[str]           = []   # appliance_category_id list

class UpdateBrandRequest(BaseModel):
    name:             Optional[str]       = None
    logo_url:         Optional[str]       = None
    is_active:        Optional[bool]      = None
    category_ids:     Optional[List[str]] = None  # None = don't change; [] = remove all

class CreateTypeRequest(BaseModel):
    name:                  str
    appliance_category_id: Optional[str] = None   # FK → service_categories.id
    brand_id:              Optional[str] = None

class UpdateTypeRequest(BaseModel):
    name:                  Optional[str]  = None
    appliance_category_id: Optional[str]  = None
    brand_id:              Optional[str]  = None
    is_active:             Optional[bool] = None

class AddApplianceRequest(BaseModel):
    customer_id:           str
    brand_id:              Optional[str]      = None
    type_id:               Optional[str]      = None
    appliance_category_id: Optional[str]      = None  # FK → service_categories.id
    category:              Optional[str]      = None  # display label kept for compat
    model:                 Optional[str]      = None
    serial_number:         Optional[str]      = None
    purchase_date:         Optional[datetime] = None
    installation_date:     Optional[datetime] = None
    warranty_expiry:       Optional[datetime] = None
    notes:                 Optional[str]      = None
    image_url:             Optional[str]      = None

class UpdateApplianceRequest(BaseModel):
    brand_id:              Optional[str]      = None
    type_id:               Optional[str]      = None
    appliance_category_id: Optional[str]      = None
    category:              Optional[str]      = None
    model:                 Optional[str]      = None
    serial_number:         Optional[str]      = None
    purchase_date:         Optional[datetime] = None
    installation_date:     Optional[datetime] = None
    warranty_expiry:       Optional[datetime] = None
    status:                Optional[str]      = None
    notes:                 Optional[str]      = None
    image_url:             Optional[str]      = None

# ── Row helpers ────────────────────────────────────────────────────────────

def _brand_row(b, category_ids: list = None, category_names: list = None) -> dict:
    return {
        "id":             str(b.id),
        "name":           b.name,
        "logo_url":       b.logo_url,
        "is_active":      b.is_active,
        "category_ids":   category_ids or [],
        "category_names": category_names or [],
    }

async def _brand_categories(brand_id: UUID, db) -> tuple:
    """Return (list[str id], list[str name]) of categories linked to a brand.
    Returns ([], []) gracefully if brand_categories table not yet migrated."""
    try:
        from app.models.appliance import BrandCategory
        from app.models.service import ServiceCategory
        rows = (await db.execute(
            select(BrandCategory, ServiceCategory)
            .join(ServiceCategory, BrandCategory.appliance_category_id == ServiceCategory.id)
            .where(BrandCategory.brand_id == brand_id)
            .order_by(ServiceCategory.name)
        )).all()
        ids   = [str(bc.appliance_category_id) for bc, _ in rows]
        names = [sc.name for _, sc in rows]
        return ids, names
    except Exception:
        return [], []

def _type_row(t, brand_name: str = None, cat_name: str = None) -> dict:
    return {
        "id":                    str(t.id),
        "name":                  t.name,
        "appliance_category_id": str(t.appliance_category_id) if t.appliance_category_id else None,
        "category_name":         cat_name,
        "brand_id":              str(t.brand_id) if t.brand_id else None,
        "brand_name":            brand_name,
        "is_active":             t.is_active,
    }

def _appliance_row(a, brand_name=None, type_name=None, cat_name=None,
                   is_under_warranty=False, customer_name=None, customer_mobile=None) -> dict:
    return {
        "id":                    str(a.id),
        "customer_id":           str(a.customer_id),
        "customer_name":         customer_name,
        "customer_mobile":       customer_mobile,
        "brand_id":              str(a.brand_id) if a.brand_id else None,
        "brand_name":            brand_name,
        "type_id":               str(a.type_id) if a.type_id else None,
        "type_name":             type_name,
        "appliance_category_id": str(a.appliance_category_id) if a.appliance_category_id else None,
        "category_name":         cat_name,
        "category":              a.category,
        "model":                 a.model,
        "serial_number":         a.serial_number,
        "purchase_date":         a.purchase_date.isoformat()     if a.purchase_date    else None,
        "installation_date":     a.installation_date.isoformat() if a.installation_date else None,
        "warranty_expiry":       a.warranty_expiry.isoformat()   if a.warranty_expiry  else None,
        "status":                a.status.value if hasattr(a.status, "value") else (a.status or "ACTIVE"),
        "is_under_warranty":     is_under_warranty,
        "notes":                 a.notes,
        "image_url":             a.image_url,
        "is_active":             a.is_active,
        "created_at":            a.created_at.isoformat() if a.created_at else None,
    }

async def _enrich(a, db) -> dict:
    from app.models.appliance import ApplianceBrand, ApplianceType
    from app.models.service import ServiceCategory
    from app.models.customer import Customer
    brand_name = cat_name = type_name = customer_name = customer_mobile = None
    if a.brand_id:
        b = (await db.execute(select(ApplianceBrand).where(ApplianceBrand.id == a.brand_id))).scalar_one_or_none()
        if b: brand_name = b.name
    if a.type_id:
        t = (await db.execute(select(ApplianceType).where(ApplianceType.id == a.type_id))).scalar_one_or_none()
        if t: type_name = t.name
    if a.appliance_category_id:
        c = (await db.execute(select(ServiceCategory).where(ServiceCategory.id == a.appliance_category_id))).scalar_one_or_none()
        if c: cat_name = c.name
    if a.customer_id:
        try:
            cust = (await db.execute(select(Customer).where(Customer.id == a.customer_id))).scalar_one_or_none()
            if cust:
                customer_name   = cust.name
                customer_mobile = cust.mobile
        except Exception:
            pass
    tz = a.warranty_expiry.tzinfo if a.warranty_expiry else None
    now = datetime.utcnow()
    under_warranty = bool(a.warranty_expiry and a.warranty_expiry.replace(tzinfo=None) > now)
    return _appliance_row(a, brand_name, type_name, cat_name, under_warranty, customer_name, customer_mobile)

# ══════════════════════════════════════════════════════════════════════════
# STATIC routes FIRST (must come before /{id} routes)
# ══════════════════════════════════════════════════════════════════════════

# ── Categories (= service_categories, used as appliance categories) ────────
@router.get("/categories", summary="List appliance categories [Public]")
async def list_appliance_categories(db: AsyncSession = Depends(get_db)):
    """
    Returns service_categories that can serve as appliance categories.
    These are the same categories linked to domains.
    Frontend uses this for Brand / Type / Appliance forms.
    """
    from app.models.service import ServiceCategory
    cats = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.is_active == True).order_by(ServiceCategory.sort_order)
    )).scalars().all()
    return success_response(data=[
        {"id": str(c.id), "name": c.name, "icon": c.icon, "description": c.description}
        for c in cats
    ])

# ── Brands ─────────────────────────────────────────────────────────────────
@router.get("/brands", summary="List appliance brands [Public]")
async def list_brands(
    search:                Optional[str] = None,
    appliance_category_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    List brands.  Filter by appliance_category_id to get only brands
    that have types under a given category (used by customer app / domain page).
    """
    from app.models.appliance import ApplianceBrand, ApplianceType
    if appliance_category_id:
        # Only brands that have at least one active type in this category
        subq = (
            select(ApplianceType.brand_id)
            .where(
                ApplianceType.appliance_category_id == UUID(appliance_category_id),
                ApplianceType.is_active == True,
                ApplianceType.brand_id.isnot(None),
            )
            .distinct()
        )
        q = select(ApplianceBrand).where(
            ApplianceBrand.id.in_(subq),
            ApplianceBrand.is_active == True,
        )
    else:
        q = select(ApplianceBrand).where(ApplianceBrand.is_active == True)

    if search:
        q = q.where(ApplianceBrand.name.ilike(f"%{search}%"))
    brands = (await db.execute(q.order_by(ApplianceBrand.name))).scalars().all()
    result = []
    for b in brands:
        cat_ids, cat_names = await _brand_categories(b.id, db)
        result.append(_brand_row(b, cat_ids, cat_names))
    return success_response(data=result)

@router.post("/brands", summary="Create appliance brand [Admin]")
async def create_brand(
    payload: CreateBrandRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import ApplianceBrand, BrandCategory
    existing = (await db.execute(
        select(ApplianceBrand).where(ApplianceBrand.name == payload.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"Brand '{payload.name}' already exists")
    b = ApplianceBrand(name=payload.name, logo_url=payload.logo_url)
    db.add(b)
    await db.flush()  # get b.id
    for cat_id in payload.category_ids:
        db.add(BrandCategory(brand_id=b.id, appliance_category_id=UUID(cat_id)))
    await db.commit()
    cat_ids, cat_names = await _brand_categories(b.id, db)
    return success_response(data=_brand_row(b, cat_ids, cat_names), message="Brand created")

@router.put("/brands/{brand_id}", summary="Update brand [Admin]")
async def update_brand(
    brand_id: UUID,
    payload: UpdateBrandRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import ApplianceBrand, BrandCategory
    from sqlalchemy import delete as sa_delete
    b = (await db.execute(select(ApplianceBrand).where(ApplianceBrand.id == brand_id))).scalar_one_or_none()
    if not b: raise HTTPException(404, "Brand not found")
    d = payload.dict(exclude_none=True)
    cat_ids_payload = d.pop("category_ids", None)
    for k, v in d.items(): setattr(b, k, v)
    if cat_ids_payload is not None:
        await db.execute(sa_delete(BrandCategory).where(BrandCategory.brand_id == brand_id))
        for cat_id in cat_ids_payload:
            db.add(BrandCategory(brand_id=brand_id, appliance_category_id=UUID(cat_id)))
    await db.commit()
    cat_ids, cat_names = await _brand_categories(brand_id, db)
    return success_response(data=_brand_row(b, cat_ids, cat_names), message="Brand updated")

# ── Types ──────────────────────────────────────────────────────────────────
@router.get("/types", summary="List appliance types [Public]")
async def list_types(
    appliance_category_id: Optional[str] = None,
    brand_id:              Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    List types. Filter by:
    - appliance_category_id (service_categories.id) → all types for an AC category
    - brand_id → all types for Samsung
    - both → Samsung AC types
    Used by domain pages to populate booking forms.
    """
    from app.models.appliance import ApplianceType, ApplianceBrand
    from app.models.service import ServiceCategory
    q = select(ApplianceType).where(ApplianceType.is_active == True)
    if appliance_category_id:
        q = q.where(ApplianceType.appliance_category_id == UUID(appliance_category_id))
    if brand_id:
        q = q.where(ApplianceType.brand_id == UUID(brand_id))
    types = (await db.execute(q.order_by(ApplianceType.name))).scalars().all()

    # Bulk-load brand names + category names
    brand_ids = list({t.brand_id for t in types if t.brand_id})
    cat_ids   = list({t.appliance_category_id for t in types if t.appliance_category_id})
    brand_map = {}; cat_map = {}
    if brand_ids:
        brands = (await db.execute(select(ApplianceBrand).where(ApplianceBrand.id.in_(brand_ids)))).scalars().all()
        brand_map = {str(b.id): b.name for b in brands}
    if cat_ids:
        cats = (await db.execute(select(ServiceCategory).where(ServiceCategory.id.in_(cat_ids)))).scalars().all()
        cat_map = {str(c.id): c.name for c in cats}

    return success_response(data=[
        _type_row(t, brand_map.get(str(t.brand_id)), cat_map.get(str(t.appliance_category_id)))
        for t in types
    ])

@router.post("/types", summary="Create appliance type [Admin]")
async def create_type(
    payload: CreateTypeRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import ApplianceType
    from app.models.service import ServiceCategory
    cat_name = None
    if payload.appliance_category_id:
        cat = (await db.execute(
            select(ServiceCategory).where(ServiceCategory.id == UUID(payload.appliance_category_id))
        )).scalar_one_or_none()
        if not cat: raise HTTPException(400, "Invalid appliance_category_id — category not found")
        cat_name = cat.name
    t = ApplianceType(
        name                 = payload.name,
        appliance_category_id= UUID(payload.appliance_category_id) if payload.appliance_category_id else None,
        brand_id             = UUID(payload.brand_id)  if payload.brand_id  else None,
    )
    db.add(t); await db.commit()
    return success_response(data=_type_row(t, None, cat_name), message="Appliance type created")

@router.put("/types/{type_id}", summary="Update appliance type [Admin]")
async def update_type(
    type_id: UUID,
    payload: UpdateTypeRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import ApplianceType
    t = (await db.execute(select(ApplianceType).where(ApplianceType.id == type_id))).scalar_one_or_none()
    if not t: raise HTTPException(404, "Type not found")
    d = payload.dict(exclude_none=True)
    for k, v in d.items():
        if k in ("brand_id", "appliance_category_id"):
            setattr(t, k, UUID(v) if v else None)
        else:
            setattr(t, k, v)
    await db.commit()
    return success_response(data=_type_row(t), message="Type updated")

# ── Domain catalogue (public — for customer app/website) ───────────────────
@router.get("/domain/{domain_slug}", summary="Full appliance catalogue for a domain [Public]")
async def domain_catalogue(
    domain_slug: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns appliance categories → brands → types for a given domain slug.
    Customer app/website uses this to populate booking service-category pickers.

    Response shape:
    {
      "domain": {...},
      "catalogue": [
        {
          "category": { id, name, icon },
          "brands": [
            { "brand": {...}, "types": [{...}] }
          ]
        }
      ]
    }
    """
    from app.models.domain import Domain, DomainCategory
    from app.models.service import ServiceCategory
    from app.models.appliance import ApplianceBrand, ApplianceType

    # 1. Find domain
    domain = (await db.execute(
        select(Domain).where(Domain.slug == domain_slug, Domain.is_active == True)
    )).scalar_one_or_none()
    if not domain:
        raise HTTPException(404, f"Domain '{domain_slug}' not found")

    # 2. Get service_categories linked to this domain
    dc_rows = (await db.execute(
        select(DomainCategory).where(DomainCategory.domain_id == domain.id, DomainCategory.is_active == True)
    )).scalars().all()
    cat_ids = [dc.category_id for dc in dc_rows]
    if not cat_ids:
        return success_response(data={"domain": _domain_mini(domain), "catalogue": []})

    cats = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id.in_(cat_ids), ServiceCategory.is_active == True)
        .order_by(ServiceCategory.sort_order)
    )).scalars().all()

    catalogue = []
    for cat in cats:
        # 3. Get types for this category
        types = (await db.execute(
            select(ApplianceType).where(
                ApplianceType.appliance_category_id == cat.id,
                ApplianceType.is_active == True
            ).order_by(ApplianceType.name)
        )).scalars().all()

        # 4. Group types by brand
        brand_ids = list({t.brand_id for t in types if t.brand_id})
        brand_map = {}
        if brand_ids:
            brands = (await db.execute(
                select(ApplianceBrand).where(ApplianceBrand.id.in_(brand_ids), ApplianceBrand.is_active == True)
            )).scalars().all()
            brand_map = {str(b.id): b for b in brands}

        brands_entry = []
        for bid in brand_ids:
            b = brand_map.get(str(bid))
            if not b: continue
            b_types = [t for t in types if t.brand_id == bid]
            brands_entry.append({
                "brand": _brand_row(b),
                "types": [_type_row(t, b.name, cat.name) for t in b_types],
            })

        # Also include types with no brand
        unbranded = [t for t in types if not t.brand_id]
        if unbranded:
            brands_entry.append({
                "brand": None,
                "types": [_type_row(t, None, cat.name) for t in unbranded],
            })

        catalogue.append({
            "category": {"id": str(cat.id), "name": cat.name, "icon": cat.icon},
            "brands": brands_entry,
        })

    return success_response(data={"domain": _domain_mini(domain), "catalogue": catalogue})

def _domain_mini(d) -> dict:
    return {"id": str(d.id), "name": d.name, "slug": d.slug, "primary_color": d.primary_color}

# ── By customer ────────────────────────────────────────────────────────────
@router.get("/customer/{customer_id}", summary="Customer's appliances [Staff]")
async def customer_appliances(
    customer_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    items = (await db.execute(
        select(CustomerAppliance)
        .where(CustomerAppliance.customer_id == customer_id, CustomerAppliance.is_active == True)
        .order_by(CustomerAppliance.created_at.desc())
    )).scalars().all()
    return success_response(data=[await _enrich(a, db) for a in items])

# ── List all (admin/staff) ─────────────────────────────────────────────────
@router.get("", summary="List all customer appliances [Staff]")
async def list_all_appliances(
    page:                  int  = Query(1, ge=1),
    per_page:              int  = Query(20, le=100),
    search:                Optional[str] = None,
    category:              Optional[str] = None,
    appliance_category_id: Optional[str] = None,
    brand_id:              Optional[str] = None,
    status:                Optional[str] = None,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    from sqlalchemy import or_
    q = select(CustomerAppliance).where(CustomerAppliance.is_active == True)
    if search:
        like = f"%{search}%"
        q = q.where(or_(
            CustomerAppliance.model.ilike(like),
            CustomerAppliance.serial_number.ilike(like),
            CustomerAppliance.category.ilike(like),
        ))
    if category:
        q = q.where(CustomerAppliance.category == category)
    if appliance_category_id:
        q = q.where(CustomerAppliance.appliance_category_id == UUID(appliance_category_id))
    if brand_id:
        q = q.where(CustomerAppliance.brand_id == UUID(brand_id))
    if status:
        from app.models.appliance import ApplianceStatus
        try:
            q = q.where(CustomerAppliance.status == ApplianceStatus(status))
        except ValueError:
            pass

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(
        q.order_by(CustomerAppliance.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    )).scalars().all()
    return success_response(data={
        "items": [await _enrich(a, db) for a in items],
        "total": total, "page": page, "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })

# ── Add appliance ──────────────────────────────────────────────────────────
@router.post("", summary="Add appliance for customer [Staff]")
async def add_appliance(
    payload: AddApplianceRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = CustomerAppliance(
        customer_id           = UUID(payload.customer_id),
        brand_id              = UUID(payload.brand_id)              if payload.brand_id              else None,
        type_id               = UUID(payload.type_id)               if payload.type_id               else None,
        appliance_category_id = UUID(payload.appliance_category_id) if payload.appliance_category_id else None,
        category              = payload.category,
        model                 = payload.model,
        serial_number         = payload.serial_number,
        purchase_date         = payload.purchase_date,
        installation_date     = payload.installation_date,
        warranty_expiry       = payload.warranty_expiry,
        notes                 = payload.notes,
        image_url             = payload.image_url,
    )
    db.add(a); await db.commit()
    return success_response(data=await _enrich(a, db), message="Appliance added")

# ── Get detail ─────────────────────────────────────────────────────────────
@router.get("/{appliance_id}", summary="Appliance detail [Staff]")
async def get_appliance(
    appliance_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    return success_response(data=await _enrich(a, db))

# ── Update ─────────────────────────────────────────────────────────────────
@router.put("/{appliance_id}", summary="Update appliance [Staff]")
async def update_appliance(
    appliance_id: UUID,
    payload: UpdateApplianceRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    d = payload.dict(exclude_none=True)
    for k, v in d.items():
        if k in ("brand_id", "type_id", "appliance_category_id"):
            setattr(a, k, UUID(v) if v else None)
        elif k == "status":
            from app.models.appliance import ApplianceStatus
            try:
                setattr(a, k, ApplianceStatus(v))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {v}")
        else:
            setattr(a, k, v)
    await db.commit()
    return success_response(data=await _enrich(a, db), message="Appliance updated")

# ── Deactivate ─────────────────────────────────────────────────────────────
@router.delete("/{appliance_id}", summary="Deactivate appliance [Staff]")
async def deactivate_appliance(
    appliance_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    a.is_active = False; await db.commit()
    return success_response(message="Appliance deactivated")

# ── Service history ────────────────────────────────────────────────────────
@router.get("/{appliance_id}/history", summary="Appliance service history [Staff]")
async def appliance_history(
    appliance_id: UUID,
    page: int = Query(1, ge=1), per_page: int = Query(20),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import ApplianceServiceHistory, CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    q = (select(ApplianceServiceHistory)
         .where(ApplianceServiceHistory.appliance_id == appliance_id)
         .order_by(ApplianceServiceHistory.service_date.desc()))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows  = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{
            "id":             str(h.id),
            "booking_id":     str(h.booking_id) if h.booking_id else None,
            "service_date":   h.service_date.isoformat() if h.service_date else None,
            "issue_reported": h.issue_reported,
            "work_done":      h.work_done,
            "technician_id":  str(h.technician_id) if h.technician_id else None,
        } for h in rows],
        "total": total, "page": page, "per_page": per_page,
    })

# ── Warranty ───────────────────────────────────────────────────────────────
@router.get("/{appliance_id}/warranty", summary="Appliance warranty status [Staff]")
async def appliance_warranty(
    appliance_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    now = datetime.utcnow()
    is_valid  = bool(a.warranty_expiry and a.warranty_expiry.replace(tzinfo=None) > now)
    days_left = max(0, (a.warranty_expiry.replace(tzinfo=None) - now).days) if a.warranty_expiry else None
    return success_response(data={
        "appliance_id":    str(appliance_id),
        "warranty_expiry": a.warranty_expiry.isoformat() if a.warranty_expiry else None,
        "is_valid":        is_valid,
        "days_remaining":  days_left,
        "status":          "VALID" if is_valid else ("EXPIRED" if a.warranty_expiry else "NOT_SET"),
    })

# ── AMC (placeholder) ──────────────────────────────────────────────────────
@router.get("/{appliance_id}/amc", summary="Appliance AMC status [Staff]")
async def appliance_amc(
    appliance_id: UUID,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db)
):
    from app.models.appliance import CustomerAppliance
    a = (await db.execute(select(CustomerAppliance).where(CustomerAppliance.id == appliance_id))).scalar_one_or_none()
    if not a: raise HTTPException(404, "Appliance not found")
    return success_response(data={
        "appliance_id": str(appliance_id), "amc_active": False,
        "amc_plan": None, "amc_expiry": None, "note": "AMC module coming in Release 4",
    })
