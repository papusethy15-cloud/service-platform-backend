"""
Multi-City Module  —  /api/v1/cities

Endpoints per docs:
  GET    /cities                        — list cities (public)
  POST   /cities                        — create city (admin)
  GET    /cities/search                 — search by pincode / name (public)
  GET    /cities/{id}                   — city detail (public)
  PUT    /cities/{id}                   — update city (admin)
  DELETE /cities/{id}                   — deactivate city (admin)
  GET    /cities/{id}/areas             — list areas in city (public)
  POST   /cities/{id}/areas             — create area (admin)
  POST   /cities/{id}/areas/bulk        — bulk import areas (admin)
  PUT    /cities/{id}/areas/{area_id}   — update area (admin)
  DELETE /cities/{id}/areas/{area_id}   — deactivate area (admin)
  GET    /cities/{id}/zones             — list zones in city (admin)
  POST   /cities/{id}/zones             — create zone (admin)
  GET    /cities/{id}/pricing           — city pricing (public)
  PUT    /cities/{id}/pricing           — update pricing (admin)
  GET    /cities/{id}/settings          — city settings (admin)
  PUT    /cities/{id}/settings          — update settings (admin)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from uuid import UUID
from typing import Optional, List
from pydantic import BaseModel

from app.core.database import get_db
from app.models.city import City, Zone, Area, CitySettings
from app.api.deps import AdminOnly, AnyAuthenticated
from app.utils.response import success_response, iso

router = APIRouter()

# ── Schemas ───────────────────────────────────────────────────

class CreateCityRequest(BaseModel):
    name:               str
    state:              str
    country:            str   = "India"
    base_travel_charge: float = 0.0
    surge_multiplier:   float = 1.0
    latitude:           Optional[float] = None
    longitude:          Optional[float] = None
    sort_order:         int   = 0

class UpdateCityRequest(BaseModel):
    name:               Optional[str]   = None
    state:              Optional[str]   = None
    country:            Optional[str]   = None
    base_travel_charge: Optional[float] = None
    surge_multiplier:   Optional[float] = None
    latitude:           Optional[float] = None
    longitude:          Optional[float] = None
    sort_order:         Optional[int]   = None
    is_serviceable:     Optional[bool]  = None

class UpdatePricingRequest(BaseModel):
    base_travel_charge: Optional[float] = None
    surge_multiplier:   Optional[float] = None

class CreateAreaRequest(BaseModel):
    name:             str
    pincode:          Optional[str]   = None
    zone_id:          Optional[str]   = None
    latitude:         Optional[float] = None
    longitude:        Optional[float] = None
    surge_multiplier: float = 1.0

class UpdateAreaRequest(BaseModel):
    name:             Optional[str]   = None
    pincode:          Optional[str]   = None
    zone_id:          Optional[str]   = None
    latitude:         Optional[float] = None
    longitude:        Optional[float] = None
    surge_multiplier: Optional[float] = None

class BulkAreaItem(BaseModel):
    name:    str
    pincode: Optional[str] = None

class BulkAreasRequest(BaseModel):
    areas: List[BulkAreaItem]

# ── Bulk JSON Import Schemas ───────────────────────────────────
class BulkImportAreaItem(BaseModel):
    name:             str
    pincode:          Optional[str]   = None
    surge_multiplier: float           = 1.0

class BulkImportZoneItem(BaseModel):
    name:        str
    description: Optional[str]            = None
    areas:       List[BulkImportAreaItem] = []

class BulkImportCityItem(BaseModel):
    name:               str
    state:              str
    country:            str   = "India"
    base_travel_charge: float = 0.0
    zones:              List[BulkImportZoneItem] = []

class BulkImportRequest(BaseModel):
    cities: List[BulkImportCityItem]

class CreateZoneRequest(BaseModel):
    name:        str
    description: Optional[str] = None

class CitySettingsRequest(BaseModel):
    min_booking_amount:      Optional[float] = None
    max_booking_amount:      Optional[float] = None
    booking_advance_days:    Optional[int]   = None
    cancellation_window_hrs: Optional[int]   = None
    auto_assign_enabled:     Optional[bool]  = None
    notes:                   Optional[str]   = None

# ── Helpers ───────────────────────────────────────────────────

def _city_row(c: City) -> dict:
    return {
        "id":                 str(c.id),
        "name":               c.name,
        "state":              c.state,
        "country":            c.country,
        "base_travel_charge": c.base_travel_charge,
        "surge_multiplier":   c.surge_multiplier,
        "latitude":           c.latitude,
        "longitude":          c.longitude,
        "is_serviceable":     getattr(c, "is_serviceable", True),
        "sort_order":         c.sort_order,
        "is_active":          c.is_active,
        "created_at":         iso(c.created_at) if c.created_at else None,
    }

def _area_row(a: Area) -> dict:
    return {
        "id":               str(a.id),
        "city_id":          str(a.city_id),
        "zone_id":          str(a.zone_id) if a.zone_id else None,
        "name":             a.name,
        "pincode":          a.pincode,
        "latitude":         a.latitude,
        "longitude":        a.longitude,
        "surge_multiplier": a.surge_multiplier,
        "is_active":        a.is_active,
    }

# ══════════════════════════════════════════════════════════════
# ROUTE ORDER: static paths before parameterised paths
# ══════════════════════════════════════════════════════════════

# ── Bulk JSON Import (static — must be before /{city_id}) ─────
@router.post("/bulk-import", summary="Bulk import cities, zones, and areas from JSON [Admin]")
async def bulk_import_cities(
    payload: BulkImportRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    """
    Import multiple cities with their zones and areas in one call.
    Duplicate protection: existing cities/zones/areas (by name) are skipped.
    Returns a summary of what was created vs skipped.
    """
    summary = []

    for city_data in payload.cities:
        city_result = {"city": city_data.name, "city_status": "", "zones": []}

        # ── City: find or skip (duplicate by name) ────────────
        existing_city = (await db.execute(
            select(City).where(City.name == city_data.name)
        )).scalar_one_or_none()

        if existing_city:
            city = existing_city
            city_result["city_status"] = "skipped (already exists)"
        else:
            city = City(
                name=city_data.name,
                state=city_data.state,
                country=city_data.country,
                base_travel_charge=city_data.base_travel_charge,
            )
            db.add(city)
            await db.flush()  # get city.id
            db.add(CitySettings(city_id=city.id))
            city_result["city_status"] = "created"

        # ── Zones + Areas ──────────────────────────────────────
        for zone_data in city_data.zones:
            zone_result = {"zone": zone_data.name, "zone_status": "", "areas": []}

            # Zone: find or skip (duplicate by name within city)
            existing_zone = (await db.execute(
                select(Zone).where(Zone.city_id == city.id, Zone.name == zone_data.name)
            )).scalar_one_or_none()

            if existing_zone:
                zone = existing_zone
                zone_result["zone_status"] = "skipped (already exists)"
            else:
                zone = Zone(
                    city_id=city.id,
                    name=zone_data.name,
                    description=zone_data.description,
                )
                db.add(zone)
                await db.flush()  # get zone.id
                zone_result["zone_status"] = "created"

            # Areas: find or skip (duplicate by name within city)
            for area_data in zone_data.areas:
                existing_area = (await db.execute(
                    select(Area).where(Area.city_id == city.id, Area.name == area_data.name)
                )).scalar_one_or_none()

                if existing_area:
                    zone_result["areas"].append({"name": area_data.name, "status": "skipped (already exists)"})
                else:
                    area = Area(
                        city_id=city.id,
                        zone_id=zone.id,
                        name=area_data.name,
                        pincode=area_data.pincode,
                        surge_multiplier=area_data.surge_multiplier,
                    )
                    db.add(area)
                    zone_result["areas"].append({"name": area_data.name, "status": "created"})

            city_result["zones"].append(zone_result)

        summary.append(city_result)

    await db.commit()
    return success_response(data={"import_summary": summary}, message="Bulk import completed")

# ── Search (static — must be before /{city_id}) ───────────────
@router.get("/search", summary="Search cities / areas by name or pincode [Public]")
async def search_cities(
    q: str = Query(..., min_length=2, description="Name or pincode to search"),
    db: AsyncSession = Depends(get_db)
):
    cities = (await db.execute(
        select(City).where(City.is_active == True,
            or_(City.name.ilike(f"%{q}%"), City.state.ilike(f"%{q}%")))
        .order_by(City.sort_order).limit(20)
    )).scalars().all()

    areas = (await db.execute(
        select(Area).where(Area.is_active == True,
            or_(Area.name.ilike(f"%{q}%"), Area.pincode.ilike(f"%{q}%")))
        .limit(30)
    )).scalars().all()

    return success_response(data={
        "cities": [_city_row(c) for c in cities],
        "areas":  [_area_row(a) for a in areas],
    })

# ── List cities ───────────────────────────────────────────────
@router.get("", summary="List all cities [Public]")
async def list_cities(
    serviceable_only: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    q = select(City).where(City.is_active == True)
    if serviceable_only:
        q = q.where(City.is_serviceable == True)
    cities = (await db.execute(q.order_by(City.sort_order, City.name))).scalars().all()

    # Attach area counts
    result = []
    for c in cities:
        area_count = (await db.execute(
            select(func.count(Area.id)).where(Area.city_id == c.id, Area.is_active == True)
        )).scalar_one()
        row = _city_row(c)
        row["area_count"] = area_count
        result.append(row)

    return success_response(data=result)

# ── Create city ───────────────────────────────────────────────
@router.post("", summary="Create city [Admin]")
async def create_city(
    payload: CreateCityRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    existing = (await db.execute(
        select(City).where(City.name == payload.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="City already exists")

    city = City(**payload.model_dump())
    db.add(city)
    await db.flush()

    # Auto-create default settings row
    db.add(CitySettings(city_id=city.id))
    await db.commit()

    row = _city_row(city)
    row["area_count"] = 0
    return success_response(data=row, message="City created")

# ── City detail ───────────────────────────────────────────────
@router.get("/{city_id}", summary="City details [Public]")
async def get_city(city_id: UUID, db: AsyncSession = Depends(get_db)):
    city = (await db.execute(
        select(City).where(City.id == city_id, City.is_active == True)
    )).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    area_count = (await db.execute(
        select(func.count(Area.id)).where(Area.city_id == city_id, Area.is_active == True)
    )).scalar_one()

    row = _city_row(city)
    row["area_count"] = area_count
    return success_response(data=row)

# ── Update city ───────────────────────────────────────────────
@router.put("/{city_id}", summary="Update city [Admin]")
async def update_city(
    city_id: UUID,
    payload: UpdateCityRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    for f, v in payload.model_dump(exclude_none=True).items():
        setattr(city, f, v)
    await db.commit()
    return success_response(data=_city_row(city), message="City updated")

# ── Deactivate city ───────────────────────────────────────────
@router.delete("/{city_id}", summary="Deactivate city [Admin]")
async def deactivate_city(
    city_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    city.is_active = False
    await db.commit()
    return success_response(message="City deactivated")

# ── Pricing ───────────────────────────────────────────────────
@router.get("/{city_id}/pricing", summary="City pricing [Public]")
async def get_city_pricing(city_id: UUID, db: AsyncSession = Depends(get_db)):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    return success_response(data={
        "city_id":            str(city_id),
        "city_name":          city.name,
        "base_travel_charge": city.base_travel_charge,
        "surge_multiplier":   city.surge_multiplier,
    })

@router.put("/{city_id}/pricing", summary="Update city pricing [Admin]")
async def update_city_pricing(
    city_id: UUID,
    payload: UpdatePricingRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    if payload.base_travel_charge is not None: city.base_travel_charge = payload.base_travel_charge
    if payload.surge_multiplier   is not None: city.surge_multiplier   = payload.surge_multiplier
    await db.commit()
    return success_response(data={
        "city_id":            str(city_id),
        "base_travel_charge": city.base_travel_charge,
        "surge_multiplier":   city.surge_multiplier,
    }, message="Pricing updated")

# ── Settings ──────────────────────────────────────────────────
@router.get("/{city_id}/settings", summary="City operational settings [Admin]")
async def get_city_settings(
    city_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    s = (await db.execute(
        select(CitySettings).where(CitySettings.city_id == city_id, CitySettings.is_active == True)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found for this city")
    return success_response(data={
        "id": str(s.id), "city_id": str(s.city_id),
        "min_booking_amount": s.min_booking_amount,
        "max_booking_amount": s.max_booking_amount,
        "booking_advance_days": s.booking_advance_days,
        "cancellation_window_hrs": s.cancellation_window_hrs,
        "auto_assign_enabled": s.auto_assign_enabled,
        "notes": s.notes,
    })

@router.put("/{city_id}/settings", summary="Update city settings [Admin]")
async def update_city_settings(
    city_id: UUID,
    payload: CitySettingsRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    s = (await db.execute(
        select(CitySettings).where(CitySettings.city_id == city_id)
    )).scalar_one_or_none()
    if not s:
        s = CitySettings(city_id=city_id)
        db.add(s)
    for f, v in payload.model_dump(exclude_none=True).items():
        setattr(s, f, v)
    await db.commit()
    return success_response(message="City settings updated")

# ── Zones ─────────────────────────────────────────────────────
@router.get("/{city_id}/zones", summary="List zones in a city [Admin]")
async def list_zones(city_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    zones = (await db.execute(
        select(Zone).where(Zone.city_id == city_id, Zone.is_active == True).order_by(Zone.name)
    )).scalars().all()
    return success_response(data=[{
        "id": str(z.id), "city_id": str(z.city_id),
        "name": z.name, "description": z.description,
    } for z in zones])

@router.post("/{city_id}/zones", summary="Create zone [Admin]")
async def create_zone(
    city_id: UUID,
    payload: CreateZoneRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    zone = Zone(city_id=city_id, name=payload.name, description=payload.description)
    db.add(zone)
    await db.commit()
    return success_response(data={"id": str(zone.id), "name": zone.name}, message="Zone created")

# ── Areas ─────────────────────────────────────────────────────
@router.get("/{city_id}/areas", summary="List areas in a city [Public]")
async def list_areas(
    city_id: UUID,
    zone_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    q = select(Area).where(Area.city_id == city_id, Area.is_active == True)
    if zone_id:
        q = q.where(Area.zone_id == UUID(zone_id))
    areas = (await db.execute(q.order_by(Area.name))).scalars().all()
    return success_response(data=[_area_row(a) for a in areas])

@router.post("/{city_id}/areas", summary="Create area [Admin]")
async def create_area(
    city_id: UUID,
    payload: CreateAreaRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    area = Area(
        city_id          = city_id,
        zone_id          = UUID(payload.zone_id) if payload.zone_id else None,
        name             = payload.name,
        pincode          = payload.pincode,
        latitude         = payload.latitude,
        longitude        = payload.longitude,
        surge_multiplier = payload.surge_multiplier,
    )
    db.add(area)
    await db.commit()
    return success_response(data=_area_row(area), message="Area created")

@router.post("/{city_id}/areas/bulk", summary="Bulk import areas [Admin]")
async def bulk_create_areas(
    city_id: UUID,
    payload: BulkAreasRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    city = (await db.execute(select(City).where(City.id == city_id))).scalar_one_or_none()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    created = []
    for item in payload.areas:
        area = Area(city_id=city_id, name=item.name, pincode=item.pincode)
        db.add(area)
        created.append(item.name)
    await db.commit()
    return success_response(
        data={"created": len(created), "names": created},
        message=f"{len(created)} areas imported"
    )

@router.put("/{city_id}/areas/{area_id}", summary="Update area [Admin]")
async def update_area(
    city_id: UUID,
    area_id: UUID,
    payload: UpdateAreaRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    area = (await db.execute(
        select(Area).where(Area.id == area_id, Area.city_id == city_id)
    )).scalar_one_or_none()
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")
    for f, v in payload.model_dump(exclude_none=True).items():
        if f == "zone_id":
            setattr(area, f, UUID(v) if v else None)
        else:
            setattr(area, f, v)
    await db.commit()
    return success_response(data=_area_row(area), message="Area updated")

@router.delete("/{city_id}/areas/{area_id}", summary="Deactivate area [Admin]")
async def deactivate_area(
    city_id: UUID,
    area_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    area = (await db.execute(
        select(Area).where(Area.id == area_id, Area.city_id == city_id)
    )).scalar_one_or_none()
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")
    area.is_active = False
    await db.commit()
    return success_response(message="Area deactivated")
