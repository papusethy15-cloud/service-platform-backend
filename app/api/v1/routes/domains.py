"""
Domain Management Module — /api/v1/domains

Docs spec:
  GET    /domains                           list domains
  POST   /domains                           create domain
  GET    /domains/{id}                      domain details + stats
  PUT    /domains/{id}                      update domain
  DELETE /domains/{id}                      deactivate domain
  GET    /domains/{id}/services             list services linked to domain
  POST   /domains/{id}/services             link service to domain
  DELETE /domains/{id}/services/{ds_id}     unlink service from domain
  GET    /domains/{id}/categories           list categories linked to domain
  POST   /domains/{id}/categories           link category to domain
  DELETE /domains/{id}/categories/{dc_id}   unlink category from domain
  GET    /domains/{id}/cities               list cities linked to domain
  POST   /domains/{id}/cities               link city to domain
  DELETE /domains/{id}/cities/{dc_id}       unlink city from domain
  GET    /domains/{id}/seo                  SEO settings
  PUT    /domains/{id}/seo                  update SEO settings

NOTE: /domains/services/{id}/city-prices routes REMOVED — they belong
      in /services/{id}/city-prices (services.py) to avoid route conflict.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from typing import Optional
from pydantic import BaseModel

from app.core.database import get_db
from app.models.domain import Domain, DomainCategory, DomainService, DomainCity, DomainSeo, DomainServiceOverride
from app.models.service import Service, ServiceCategory
from app.models.city import City
from app.api.deps import AdminOnly, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

# ── Schemas ───────────────────────────────────────────────────
class CreateDomainRequest(BaseModel):
    name:          str
    slug:          str
    description:   Optional[str] = None
    logo_url:      Optional[str] = None
    primary_color: Optional[str] = "#1B4FD8"
    meta_title:    Optional[str] = None
    meta_desc:     Optional[str] = None
    sort_order:    int = 0

class UpdateDomainRequest(BaseModel):
    name:          Optional[str] = None
    slug:          Optional[str] = None
    description:   Optional[str] = None
    logo_url:      Optional[str] = None
    primary_color: Optional[str] = None
    meta_title:    Optional[str] = None
    meta_desc:     Optional[str] = None
    sort_order:    Optional[int] = None
    is_active:     Optional[bool] = None

class LinkServiceRequest(BaseModel):
    service_id:  str
    is_featured: bool = False

class LinkCategoryRequest(BaseModel):
    category_id: str
    sort_order:  int = 0

class LinkCityRequest(BaseModel):
    city_id:    str
    sort_order: int = 0

class BulkLinkRequest(BaseModel):
    service_ids: list

class SeoRequest(BaseModel):
    meta_title:       Optional[str] = None
    meta_description: Optional[str] = None
    meta_keywords:    Optional[str] = None
    og_title:         Optional[str] = None
    og_description:   Optional[str] = None
    og_image_url:     Optional[str] = None
    canonical_url:    Optional[str] = None
    robots:           Optional[str] = "index,follow"
    schema_json:      Optional[str] = None

# ── Helpers ───────────────────────────────────────────────────
def _domain_row(d: Domain, service_count: int = 0, category_count: int = 0, city_count: int = 0) -> dict:
    return {
        "id":            str(d.id),
        "name":          d.name,
        "slug":          d.slug,
        "description":   d.description,
        "logo_url":      d.logo_url,
        "primary_color": d.primary_color,
        "meta_title":    d.meta_title,
        "meta_desc":     d.meta_desc,
        "sort_order":    d.sort_order,
        "is_active":     d.is_active,
        "service_count": service_count,
        "category_count":category_count,
        "city_count":    city_count,
        "created_at":    d.created_at.isoformat() if d.created_at else None,
    }

# ══════════════════════════════════════════════════════════════
# ROUTES — static paths before parameterised paths
# ══════════════════════════════════════════════════════════════

# ── List domains ──────────────────────────────────────────────
@router.get("", summary="List domains [Public]")
async def list_domains(db: AsyncSession = Depends(get_db)):
    domains = (await db.execute(
        select(Domain).where(Domain.is_active == True).order_by(Domain.sort_order)
    )).scalars().all()

    result = []
    for d in domains:
        svc_count = (await db.execute(
            select(func.count(DomainService.id))
            .where(DomainService.domain_id == d.id, DomainService.is_active == True)
        )).scalar_one()
        cat_count = (await db.execute(
            select(func.count(DomainCategory.id))
            .where(DomainCategory.domain_id == d.id, DomainCategory.is_active == True)
        )).scalar_one()
        city_count = (await db.execute(
            select(func.count(DomainCity.id))
            .where(DomainCity.domain_id == d.id, DomainCity.is_active == True)
        )).scalar_one()
        result.append(_domain_row(d, svc_count, cat_count, city_count))

    return success_response(data={"items": result, "total": len(result)})

# ── Create domain ─────────────────────────────────────────────
@router.post("", summary="Create domain [Admin]")
async def create_domain(
    payload: CreateDomainRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    existing = (await db.execute(
        select(Domain).where(Domain.slug == payload.slug)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Domain slug already exists")

    domain = Domain(**payload.model_dump())
    db.add(domain)
    await db.flush()
    # Auto-create empty SEO row
    db.add(DomainSeo(domain_id=domain.id))
    await db.commit()

    return success_response(
        data=_domain_row(domain),
        message="Domain created"
    )

# ── Get domain detail ─────────────────────────────────────────
@router.get("/{domain_id}", summary="Domain details + stats [Public]")
async def get_domain(domain_id: UUID, db: AsyncSession = Depends(get_db)):
    d = (await db.execute(
        select(Domain).where(Domain.id == domain_id)
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")

    svc_count = (await db.execute(
        select(func.count(DomainService.id))
        .where(DomainService.domain_id == domain_id, DomainService.is_active == True)
    )).scalar_one()
    cat_count = (await db.execute(
        select(func.count(DomainCategory.id))
        .where(DomainCategory.domain_id == domain_id, DomainCategory.is_active == True)
    )).scalar_one()
    city_count = (await db.execute(
        select(func.count(DomainCity.id))
        .where(DomainCity.domain_id == domain_id, DomainCity.is_active == True)
    )).scalar_one()

    return success_response(data=_domain_row(d, svc_count, cat_count, city_count))

# ── Update domain ─────────────────────────────────────────────
@router.put("/{domain_id}", summary="Update domain [Admin]")
async def update_domain(
    domain_id: UUID,
    payload:   UpdateDomainRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    d = (await db.execute(select(Domain).where(Domain.id == domain_id))).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    # Slug uniqueness check if slug is being changed
    if payload.slug and payload.slug != d.slug:
        clash = (await db.execute(
            select(Domain).where(Domain.slug == payload.slug, Domain.id != domain_id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=400, detail="Slug already in use by another domain")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(d, k, v)
    await db.commit()
    return success_response(data=_domain_row(d), message="Domain updated")

# ── Deactivate domain ─────────────────────────────────────────
@router.delete("/{domain_id}", summary="Deactivate domain [Admin]")
async def deactivate_domain(
    domain_id: UUID,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    d = (await db.execute(select(Domain).where(Domain.id == domain_id))).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    d.is_active = False
    await db.commit()
    return success_response(message="Domain deactivated")

# ── Domain services ───────────────────────────────────────────
@router.get("/{domain_id}/services", summary="Services linked to domain [Public]")
async def get_domain_services(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    # Single JOIN query — no N+1. LEFT JOINs the per-domain override so the
    # storefront grid can show the admin-uploaded service image without a
    # separate round trip per card (the override row may not exist yet for
    # every linked service, hence outerjoin + nullable columns below).
    rows = (await db.execute(
        select(
            DomainService, Service, ServiceCategory.name.label("cat_name"),
            DomainServiceOverride.image_url.label("override_image"),
            DomainServiceOverride.thumbnail_url.label("override_thumb"),
        )
        .join(Service, Service.id == DomainService.service_id)
        .join(ServiceCategory, ServiceCategory.id == Service.category_id)
        .outerjoin(DomainServiceOverride, DomainServiceOverride.domain_service_id == DomainService.id)
        .where(
            DomainService.domain_id == domain_id,
            DomainService.is_active == True,
            Service.is_active == True,
        )
        .order_by(ServiceCategory.sort_order, Service.sort_order)
    )).all()

    return success_response(data=[{
        "domain_service_id": str(r.DomainService.id),
        "service_id":        str(r.Service.id),
        "name":              r.Service.name,
        "description":       r.Service.description,
        "category_id":       str(r.Service.category_id),
        "category_name":     r.cat_name,
        "base_price":        r.Service.base_price,
        "gst_percent":       r.Service.gst_percent,
        "duration_mins":     r.Service.duration_mins,
        "is_featured":       r.DomainService.is_featured,
        "is_visible":        r.Service.is_visible,
        "image_url":         r.override_image,
        "thumbnail_url":     r.override_thumb,
    } for r in rows])

@router.post("/{domain_id}/services", summary="Link service to domain [Admin]")
async def add_domain_service(
    domain_id: UUID,
    payload:   LinkServiceRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    svc = (await db.execute(
        select(Service).where(Service.id == UUID(payload.service_id), Service.is_active == True)
    )).scalar_one_or_none()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")

    existing = (await db.execute(
        select(DomainService).where(
            DomainService.domain_id  == domain_id,
            DomainService.service_id == UUID(payload.service_id),
        )
    )).scalar_one_or_none()

    if existing:
        existing.is_active  = True
        existing.is_featured = payload.is_featured
        msg = "Service re-linked to domain"
    else:
        existing = DomainService(
            domain_id  = domain_id,
            service_id = UUID(payload.service_id),
            is_featured = payload.is_featured,
        )
        db.add(existing)
        msg = "Service linked to domain"

    await db.commit()
    return success_response(
        data={"domain_service_id": str(existing.id), "service_id": str(existing.service_id)},
        message=msg
    )

@router.patch("/{domain_id}/services/{ds_id}", summary="Toggle featured on linked service [Admin]")
async def toggle_featured(
    domain_id: UUID,
    ds_id:     UUID,
    payload:   dict = None,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    ds = (await db.execute(
        select(DomainService).where(DomainService.id == ds_id, DomainService.domain_id == domain_id)
    )).scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Link not found")
    ds.is_featured = not ds.is_featured
    await db.commit()
    return success_response(
        data={"domain_service_id": str(ds.id), "is_featured": ds.is_featured},
        message="Featured toggled"
    )

@router.delete("/{domain_id}/services/{ds_id}", summary="Unlink service from domain [Admin]")
async def remove_domain_service(
    domain_id: UUID,
    ds_id:     UUID,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    ds = (await db.execute(
        select(DomainService).where(DomainService.id == ds_id, DomainService.domain_id == domain_id)
    )).scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Link not found")
    ds.is_active = False
    await db.commit()
    return success_response(message="Service removed from domain")

# ── Domain categories ─────────────────────────────────────────


@router.post("/{domain_id}/services/bulk", summary="Bulk link services to domain [Admin]")
async def bulk_add_domain_services(
    domain_id: UUID,
    payload:   "BulkLinkRequest",
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    """Link multiple services to a domain in one request."""
    linked = []
    skipped = []
    for service_id in payload.service_ids:
        svc = (await db.execute(
            select(Service).where(Service.id == UUID(service_id), Service.is_active == True)
        )).scalar_one_or_none()
        if not svc:
            skipped.append(service_id)
            continue
        existing = (await db.execute(
            select(DomainService).where(
                DomainService.domain_id  == domain_id,
                DomainService.service_id == UUID(service_id),
            )
        )).scalar_one_or_none()
        if existing:
            existing.is_active = True
        else:
            db.add(DomainService(domain_id=domain_id, service_id=UUID(service_id), is_featured=False))
        linked.append(service_id)
    await db.commit()
    return success_response(
        data={"linked": len(linked), "skipped": len(skipped)},
        message=f"{len(linked)} service(s) linked to domain"
    )

@router.get("/{domain_id}/categories", summary="Categories linked to domain [Public]")
async def get_domain_categories(domain_id: UUID, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(DomainCategory, ServiceCategory)
        .join(ServiceCategory, ServiceCategory.id == DomainCategory.category_id)
        .where(
            DomainCategory.domain_id == domain_id,
            DomainCategory.is_active == True,
            ServiceCategory.is_active == True,
        )
        .order_by(DomainCategory.sort_order, ServiceCategory.sort_order)
    )).all()

    return success_response(data=[{
        "domain_category_id": str(r.DomainCategory.id),
        "category_id":        str(r.ServiceCategory.id),
        "name":               r.ServiceCategory.name,
        "description":        r.ServiceCategory.description,
        "icon":               r.ServiceCategory.icon,
        "sort_order":         r.DomainCategory.sort_order,
    } for r in rows])

@router.post("/{domain_id}/categories", summary="Link category to domain [Admin]")
async def add_domain_category(
    domain_id: UUID,
    payload:   LinkCategoryRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    cat = (await db.execute(
        select(ServiceCategory).where(ServiceCategory.id == UUID(payload.category_id), ServiceCategory.is_active == True)
    )).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    existing = (await db.execute(
        select(DomainCategory).where(
            DomainCategory.domain_id   == domain_id,
            DomainCategory.category_id == UUID(payload.category_id),
        )
    )).scalar_one_or_none()

    if existing:
        existing.is_active  = True
        existing.sort_order = payload.sort_order
        msg = "Category re-linked"
    else:
        existing = DomainCategory(
            domain_id   = domain_id,
            category_id = UUID(payload.category_id),
            sort_order  = payload.sort_order,
        )
        db.add(existing)
        msg = "Category linked to domain"

    await db.commit()
    return success_response(
        data={"domain_category_id": str(existing.id), "category_id": str(existing.category_id)},
        message=msg
    )

@router.delete("/{domain_id}/categories/{dc_id}", summary="Unlink category from domain [Admin]")
async def remove_domain_category(
    domain_id: UUID,
    dc_id:     UUID,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    dc = (await db.execute(
        select(DomainCategory).where(DomainCategory.id == dc_id, DomainCategory.domain_id == domain_id)
    )).scalar_one_or_none()
    if not dc:
        raise HTTPException(status_code=404, detail="Link not found")
    dc.is_active = False
    await db.commit()
    return success_response(message="Category removed from domain")

# ── Cities linked to domain ─────────────────────────────────────
@router.get("/{domain_id}/cities", summary="Cities linked to domain [Public]")
async def get_domain_cities(domain_id: UUID, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(DomainCity, City)
        .join(City, City.id == DomainCity.city_id)
        .where(
            DomainCity.domain_id == domain_id,
            DomainCity.is_active == True,
            City.is_active == True,
        )
        .order_by(DomainCity.sort_order, City.sort_order)
    )).all()

    return success_response(data=[{
        "domain_city_id": str(r.DomainCity.id),
        "city_id":        str(r.City.id),
        "name":           r.City.name,
        "state":          r.City.state,
        "is_serviceable": getattr(r.City, "is_serviceable", True),
        "sort_order":     r.DomainCity.sort_order,
    } for r in rows])

@router.post("/{domain_id}/cities", summary="Link city to domain [Admin]")
async def add_domain_city(
    domain_id: UUID,
    payload:   LinkCityRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(
        select(City).where(City.id == UUID(payload.city_id), City.is_active == True)
    )).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    existing = (await db.execute(
        select(DomainCity).where(
            DomainCity.domain_id == domain_id,
            DomainCity.city_id   == UUID(payload.city_id),
        )
    )).scalar_one_or_none()

    if existing:
        existing.is_active  = True
        existing.sort_order = payload.sort_order
        msg = "City re-linked"
    else:
        existing = DomainCity(
            domain_id  = domain_id,
            city_id    = UUID(payload.city_id),
            sort_order = payload.sort_order,
        )
        db.add(existing)
        msg = "City linked to domain"

    await db.commit()
    return success_response(
        data={"domain_city_id": str(existing.id), "city_id": str(existing.city_id)},
        message=msg
    )

@router.delete("/{domain_id}/cities/{dc_id}", summary="Unlink city from domain [Admin]")
async def remove_domain_city(
    domain_id: UUID,
    dc_id:     UUID,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    dc = (await db.execute(
        select(DomainCity).where(DomainCity.id == dc_id, DomainCity.domain_id == domain_id)
    )).scalar_one_or_none()
    if not dc:
        raise HTTPException(status_code=404, detail="Link not found")
    dc.is_active = False
    await db.commit()
    return success_response(message="City removed from domain")

# ── SEO settings ──────────────────────────────────────────────
@router.get("/{domain_id}/seo", summary="Domain SEO settings [Admin]")
async def get_domain_seo(domain_id: UUID, db: AsyncSession = Depends(get_db)):
    seo = (await db.execute(
        select(DomainSeo).where(DomainSeo.domain_id == domain_id, DomainSeo.is_active == True)
    )).scalar_one_or_none()
    if not seo:
        # Auto-create on first access
        seo = DomainSeo(domain_id=domain_id)
        db.add(seo); await db.commit()
    return success_response(data={
        "id":              str(seo.id),
        "domain_id":       str(seo.domain_id),
        "meta_title":      seo.meta_title,
        "meta_description":seo.meta_description,
        "meta_keywords":   seo.meta_keywords,
        "og_title":        seo.og_title,
        "og_description":  seo.og_description,
        "og_image_url":    seo.og_image_url,
        "canonical_url":   seo.canonical_url,
        "robots":          seo.robots,
        "schema_json":     seo.schema_json,
    })

@router.put("/{domain_id}/seo", summary="Update domain SEO settings [Admin]")
async def update_domain_seo(
    domain_id: UUID,
    payload:   SeoRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    seo = (await db.execute(
        select(DomainSeo).where(DomainSeo.domain_id == domain_id)
    )).scalar_one_or_none()
    if not seo:
        seo = DomainSeo(domain_id=domain_id)
        db.add(seo)
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(seo, k, v)
    await db.commit()
    return success_response(message="SEO settings updated")


# ══════════════════════════════════════════════════════════════
# DOMAIN PROFILE — rich branding, social, address, invoice data
# ══════════════════════════════════════════════════════════════
from app.models.domain import DomainProfile as _DomainProfile

class DomainProfileRequest(BaseModel):
    # Media
    logo_url:           Optional[str] = None
    logo_dark_url:      Optional[str] = None
    favicon_url:        Optional[str] = None
    og_image_url:       Optional[str] = None
    banner_url:         Optional[str] = None
    # Social
    facebook_url:       Optional[str] = None
    instagram_url:      Optional[str] = None
    twitter_url:        Optional[str] = None
    youtube_url:        Optional[str] = None
    linkedin_url:       Optional[str] = None
    whatsapp_number:    Optional[str] = None
    # Contact
    support_phone:      Optional[str] = None
    support_email:      Optional[str] = None
    office_address:     Optional[str] = None
    office_city:        Optional[str] = None
    office_state:       Optional[str] = None
    office_pincode:     Optional[str] = None
    office_country:     Optional[str] = None
    google_maps_url:    Optional[str] = None
    # Invoice / business
    business_legal_name:  Optional[str] = None
    gstin:                Optional[str] = None
    pan_number:           Optional[str] = None
    invoice_prefix:       Optional[str] = None
    bank_account_name:    Optional[str] = None
    bank_account_number:  Optional[str] = None
    bank_ifsc:            Optional[str] = None
    bank_name:            Optional[str] = None
    bank_branch:          Optional[str] = None
    upi_id:               Optional[str] = None
    # About
    tagline:              Optional[str] = None
    about_short:          Optional[str] = None
    copyright_text:       Optional[str] = None


def _profile_row(p: _DomainProfile) -> dict:
    return {
        "id": str(p.id), "domain_id": str(p.domain_id),
        # media
        "logo_url": p.logo_url, "logo_dark_url": p.logo_dark_url,
        "favicon_url": p.favicon_url, "og_image_url": p.og_image_url,
        "banner_url": p.banner_url,
        # social
        "facebook_url": p.facebook_url, "instagram_url": p.instagram_url,
        "twitter_url": p.twitter_url, "youtube_url": p.youtube_url,
        "linkedin_url": p.linkedin_url, "whatsapp_number": p.whatsapp_number,
        # contact
        "support_phone": p.support_phone, "support_email": p.support_email,
        "office_address": p.office_address, "office_city": p.office_city,
        "office_state": p.office_state, "office_pincode": p.office_pincode,
        "office_country": p.office_country, "google_maps_url": p.google_maps_url,
        # invoice
        "business_legal_name": p.business_legal_name, "gstin": p.gstin,
        "pan_number": p.pan_number, "invoice_prefix": p.invoice_prefix,
        "bank_account_name": p.bank_account_name, "bank_account_number": p.bank_account_number,
        "bank_ifsc": p.bank_ifsc, "bank_name": p.bank_name, "bank_branch": p.bank_branch,
        "upi_id": p.upi_id,
        # about
        "tagline": p.tagline, "about_short": p.about_short, "copyright_text": p.copyright_text,
    }


@router.get("/{domain_id}/profile", summary="Domain profile [Public]")
async def get_domain_profile(domain_id: UUID, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(
        select(_DomainProfile).where(_DomainProfile.domain_id == domain_id)
    )).scalar_one_or_none()
    if not profile:
        # Auto-create empty profile on first access
        profile = _DomainProfile(domain_id=domain_id)
        db.add(profile)
        await db.commit()
    return success_response(data=_profile_row(profile))


@router.put("/{domain_id}/profile", summary="Update domain profile [Admin]")
async def update_domain_profile(
    domain_id: UUID,
    payload: DomainProfileRequest,
    current_user=Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    profile = (await db.execute(
        select(_DomainProfile).where(_DomainProfile.domain_id == domain_id)
    )).scalar_one_or_none()
    if not profile:
        profile = _DomainProfile(domain_id=domain_id)
        db.add(profile)
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(profile, k, v)
    await db.commit()
    return success_response(data=_profile_row(profile), message="Domain profile updated")


# ══════════════════════════════════════════════════════════════
# DOMAIN SERVICE OVERRIDES — per-domain image + SEO per service
# ══════════════════════════════════════════════════════════════
from app.models.domain import DomainServiceOverride

class DomainServiceOverrideRequest(BaseModel):
    image_url:        Optional[str] = None
    thumbnail_url:    Optional[str] = None
    meta_title:       Optional[str] = None
    meta_description: Optional[str] = None
    meta_keywords:    Optional[str] = None
    og_title:         Optional[str] = None
    og_description:   Optional[str] = None
    og_image_url:     Optional[str] = None
    # canonical_url, robots, schema_json are auto-generated by the frontend
    includes_json:    Optional[str] = None   # JSON array of strings
    excludes_json:    Optional[str] = None   # JSON array of strings
    faqs_json:        Optional[str] = None   # JSON array of {q, a} objects


def _override_row(o: DomainServiceOverride) -> dict:
    import json
    def _json_list(v):
        if not v: return []
        try: return json.loads(v)
        except: return []
    def _json_faqs(v):
        if not v: return []
        try: return json.loads(v)
        except: return []
    return {
        "id":               str(o.id),
        "domain_service_id":str(o.domain_service_id),
        "image_url":        o.image_url,
        "thumbnail_url":    o.thumbnail_url,
        "meta_title":       o.meta_title,
        "meta_description": o.meta_description,
        "meta_keywords":    o.meta_keywords,
        "og_title":         o.og_title,
        "og_description":   o.og_description,
        "og_image_url":     o.og_image_url,
        "includes":         _json_list(o.includes_json),
        "excludes":         _json_list(o.excludes_json),
        "faqs":             _json_faqs(o.faqs_json),
    }


@router.get("/{domain_id}/services/{ds_id}/override",
            summary="Get domain-service override (image + SEO) [Admin]")
async def get_domain_service_override(
    domain_id: UUID,
    ds_id:     UUID,
    db: AsyncSession = Depends(get_db),
):
    # Validate the domain_service belongs to this domain
    ds = (await db.execute(
        select(DomainService).where(
            DomainService.id == ds_id,
            DomainService.domain_id == domain_id,
        )
    )).scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Domain service link not found")

    override = (await db.execute(
        select(DomainServiceOverride).where(DomainServiceOverride.domain_service_id == ds_id)
    )).scalar_one_or_none()

    if not override:
        # Return empty shell — no auto-create on GET
        return success_response(data={
            "id": None, "domain_service_id": str(ds_id),
            "image_url": None, "thumbnail_url": None,
            "meta_title": None, "meta_description": None, "meta_keywords": None,
            "og_title": None, "og_description": None, "og_image_url": None,
            "includes": [], "excludes": [], "faqs": [],
        })

    return success_response(data=_override_row(override))


@router.put("/{domain_id}/services/{ds_id}/override",
            summary="Upsert domain-service override (image + SEO) [Admin]")
async def upsert_domain_service_override(
    domain_id: UUID,
    ds_id:     UUID,
    payload:   DomainServiceOverrideRequest,
    current_user = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    ds = (await db.execute(
        select(DomainService).where(
            DomainService.id == ds_id,
            DomainService.domain_id == domain_id,
        )
    )).scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Domain service link not found")

    override = (await db.execute(
        select(DomainServiceOverride).where(DomainServiceOverride.domain_service_id == ds_id)
    )).scalar_one_or_none()

    if not override:
        override = DomainServiceOverride(domain_service_id=ds_id)
        db.add(override)

    import json as _json
    payload_dict = payload.model_dump()
    for k, v in payload_dict.items():
        setattr(override, k, v)

    await db.commit()
    await db.refresh(override)
    return success_response(data=_override_row(override), message="Override saved")
