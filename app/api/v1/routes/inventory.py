"""
Inventory Routes — Full Implementation
=======================================
Covers all documented API endpoints:
  /inventory/items          CRUD, search, low-stock filter
  /inventory/categories     CRUD
  /inventory/brands         CRUD
  /inventory/warehouses     CRUD + per-warehouse stock
  /inventory/stock          aggregated stock view
  /inventory/adjust         stock adjustment (+/-)
  /inventory/transfer       warehouse → warehouse transfer
  /inventory/assign-tech    warehouse → technician assignment
  /inventory/return-tech    technician → warehouse return
  /inventory/consume        mark parts consumed in a booking
  /inventory/damage         write off damaged stock
  /inventory/movements      full ledger (paginated, filterable)
  /inventory/technician/{id}  technician's current carry stock
  /inventory/low-stock      items below reorder level
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, update as sa_update, text
from sqlalchemy.orm import selectinload
from uuid import UUID
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# Helper — lazy-import models (avoids circular at startup)
# ══════════════════════════════════════════════════════════════════════════════
def _models():
    from app.models.inventory import (
        InventoryItem, InventoryCategory, InventoryBrand,
        Warehouse, WarehouseStock, TechnicianStock,
        TechnicianStockLog, StockMovement,
        MovementType, TechnicianStockStatus, ReorderRule
    )
    return (InventoryItem, InventoryCategory, InventoryBrand,
            Warehouse, WarehouseStock, TechnicianStock,
            TechnicianStockLog, StockMovement,
            MovementType, TechnicianStockStatus, ReorderRule)


async def _get_item(item_id: UUID, db: AsyncSession):
    InventoryItem = _models()[0]
    item = (await db.execute(select(InventoryItem).where(InventoryItem.id == item_id, InventoryItem.is_active == True))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Inventory item not found")
    return item


async def _get_or_create_wh_stock(warehouse_id: UUID, item_id: UUID, db: AsyncSession):
    """Return WarehouseStock row, creating it if missing."""
    WarehouseStock = _models()[4]
    ws = (await db.execute(
        select(WarehouseStock).where(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.item_id == item_id
        )
    )).scalar_one_or_none()
    if not ws:
        ws = WarehouseStock(warehouse_id=warehouse_id, item_id=item_id, quantity=0, reserved_qty=0)
        db.add(ws)
        await db.flush()
    return ws


async def _get_or_create_tech_stock(technician_id: UUID, item_id: UUID, db: AsyncSession):
    TechnicianStock = _models()[5]
    ts = (await db.execute(
        select(TechnicianStock).where(
            TechnicianStock.technician_id == technician_id,
            TechnicianStock.item_id == item_id
        )
    )).scalar_one_or_none()
    if not ts:
        ts = TechnicianStock(technician_id=technician_id, item_id=item_id, quantity=0)
        db.add(ts)
        await db.flush()
    return ts

# ── Multi-category helpers ─────────────────────────────────────────────────

async def _get_item_categories(item_ids: list, db) -> dict:
    """Returns {item_id_str: [{"id":..,"name":..}]} for all given item IDs."""
    if not item_ids:
        return {}
    r = await db.execute(
        text("""
            SELECT isc.item_id, sc.id AS cat_id, sc.name, sc.icon
            FROM item_service_categories isc
            JOIN service_categories sc ON sc.id = isc.category_id
            WHERE isc.item_id = ANY(:ids)
            ORDER BY sc.sort_order, sc.name
        """),
        {"ids": item_ids}
    )
    rows = r.fetchall()
    result: dict = {}
    for row in rows:
        key = str(row[0])
        if key not in result:
            result[key] = []
        result[key].append({"id": str(row[1]), "name": row[2], "icon": row[3] or ""})
    return result


async def _sync_item_categories(item_id, category_ids: list, db):
    """
    Replaces all item_service_categories rows for this item with the new list.
    Safe to call with empty list (removes all categories).
    """
    from uuid import UUID as _UUID
    await db.execute(
        text("DELETE FROM item_service_categories WHERE item_id = :iid"),
        {"iid": item_id}
    )
    for cid in (category_ids or []):
        if cid:
            await db.execute(
                text("INSERT INTO item_service_categories (item_id, category_id) "
                     "VALUES (:iid, :cid) ON CONFLICT DO NOTHING"),
                {"iid": item_id, "cid": _UUID(str(cid))}
            )


def _item_row(i, categories: list = None, brand_name: str = None):
    """
    categories: list of {"id": str, "name": str} dicts from item_service_categories.
    One item can belong to multiple service categories.
    brand_name: resolved from ApplianceBrand (passed in by callers that do the join).
    """
    available = (i.current_stock or 0) - (i.reserved_stock or 0)
    cats = categories or []
    return {
        "id": str(i.id),
        "name": i.name,
        "sku": i.sku,
        "barcode": i.barcode,
        # Legacy single-category fields (first linked category for backward compat)
        "category_id": cats[0]["id"] if cats else None,
        "category_name": cats[0]["name"] if cats else None,
        # New multi-category fields
        "category_ids": [c["id"] for c in cats],
        "categories": cats,
        "brand_id": str(i.brand_id) if i.brand_id else None,
        "brand_name": brand_name,
        "unit": i.unit,
        "description": i.description,
        "hsn_code": i.hsn_code,
        "gst_percent": i.gst_percent,
        "cost_price": i.cost_price,
        "selling_price": i.selling_price,
        "mrp": i.mrp,
        "current_stock": i.current_stock or 0,
        "reserved_stock": i.reserved_stock or 0,
        "available_stock": max(0, available),
        "min_stock_level": i.min_stock_level or 0,
        "reorder_qty": i.reorder_qty or 0,
        "is_low_stock": (i.current_stock or 0) <= (i.min_stock_level or 0),
        "is_consumable": i.is_consumable,
        "is_serialised": i.is_serialised,
        "image_url": i.image_url,
        "is_active": i.is_active,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


def _movement_row(m):
    return {
        "id": str(m.id),
        "item_id": str(m.item_id),
        "movement_type": m.movement_type.value if hasattr(m.movement_type, 'value') else m.movement_type,
        "quantity": m.quantity,
        "from_warehouse_id": str(m.from_warehouse_id) if m.from_warehouse_id else None,
        "to_warehouse_id": str(m.to_warehouse_id) if m.to_warehouse_id else None,
        "technician_id": str(m.technician_id) if m.technician_id else None,
        "booking_id": str(m.booking_id) if m.booking_id else None,
        "reference_no": m.reference_no,
        "batch_no": m.batch_no,
        "reason": m.reason,
        "notes": m.notes,
        "unit_cost": m.unit_cost,
        "performed_by": str(m.performed_by) if m.performed_by else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Pydantic Schemas
# ══════════════════════════════════════════════════════════════════════════════

class CreateCategoryRequest(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: int = 0

class UpdateCategoryRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None

class CreateBrandRequest(BaseModel):
    name: str

class CreateItemRequest(BaseModel):
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_id: Optional[str] = None    # legacy single (kept for backward compat)
    category_ids: Optional[List[str]] = None  # multi-category: one item → many service categories
    brand_id: Optional[str] = None
    unit: str = "pcs"
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    gst_percent: float = 18.0
    cost_price: float = 0.0
    selling_price: float = 0.0
    mrp: float = 0.0
    min_stock_level: int = 0
    reorder_qty: int = 0
    is_consumable: bool = False
    is_serialised: bool = False
    image_url: Optional[str] = None

class UpdateItemRequest(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_id: Optional[str] = None    # legacy single
    category_ids: Optional[List[str]] = None  # multi-category
    brand_id: Optional[str] = None
    unit: Optional[str] = None
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    gst_percent: Optional[float] = None
    cost_price: Optional[float] = None
    selling_price: Optional[float] = None
    mrp: Optional[float] = None
    min_stock_level: Optional[int] = None
    reorder_qty: Optional[int] = None
    is_consumable: Optional[bool] = None
    is_serialised: Optional[bool] = None
    image_url: Optional[str] = None

class CreateWarehouseRequest(BaseModel):
    name: str
    code: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    city_id: Optional[str] = None
    phone: Optional[str] = None
    is_default: bool = False

class UpdateWarehouseRequest(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    is_default: Optional[bool] = None

class StockOpeningRequest(BaseModel):
    """Set initial stock for an item in a warehouse (one-time opening entry)."""
    item_id: str
    warehouse_id: str
    quantity: int = Field(..., gt=0)
    unit_cost: Optional[float] = None
    notes: Optional[str] = None

class StockAdjustmentRequest(BaseModel):
    item_id: str
    warehouse_id: str
    quantity: int          # positive = add, negative = reduce
    reason: str
    notes: Optional[str] = None

class StockTransferRequest(BaseModel):
    item_id: str
    from_warehouse_id: str
    to_warehouse_id: str
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = None
    reference_no: Optional[str] = None

class AssignToTechRequest(BaseModel):
    item_id: str
    technician_id: str
    warehouse_id: str
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = None
    booking_id: Optional[str] = None   # pre-assign for a specific job

class ReturnFromTechRequest(BaseModel):
    item_id: str
    technician_id: str
    warehouse_id: str
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = None
    condition: str = "GOOD"  # GOOD | DAMAGED | LOST

class ConsumeStockRequest(BaseModel):
    """Mark stock consumed by a technician during a booking."""
    technician_id: str
    booking_id: str
    items: List[dict]    # [{item_id, quantity, unit_cost?}]
    notes: Optional[str] = None

class DamageRequest(BaseModel):
    item_id: str
    warehouse_id: Optional[str] = None
    technician_id: Optional[str] = None
    quantity: int = Field(..., gt=0)
    reason: str
    notes: Optional[str] = None

class ReorderRuleRequest(BaseModel):
    item_id: str
    warehouse_id: Optional[str] = None
    reorder_level: int
    reorder_qty: int
    preferred_vendor_id: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORIES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/categories", summary="List service categories for inventory [Staff]")
async def list_categories(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns service_categories — the single shared category taxonomy used for
    both services and inventory items. Includes item_count per category via
    the item_service_categories many-to-many table.
    """
    from app.models.service import ServiceCategory
    cats = (await db.execute(
        select(ServiceCategory)
        .where(ServiceCategory.is_active == True)
        .order_by(ServiceCategory.sort_order, ServiceCategory.name)
    )).scalars().all()

    result = []
    for c in cats:
        count = (await db.execute(
            text("SELECT COUNT(*) FROM item_service_categories isc "
                 "JOIN inventory_items ii ON ii.id = isc.item_id "
                 "WHERE isc.category_id = :cid AND ii.is_active = true"),
            {"cid": c.id}
        )).scalar_one()
        result.append({
            "id": str(c.id), "name": c.name,
            "description": c.description,
            "icon": c.icon,
            "sort_order": c.sort_order,
            "item_count": count
        })
    return success_response(data=result)


@router.post("/categories", summary="Create service category [Admin]")
async def create_category(
    payload: CreateCategoryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    """Creates a ServiceCategory — shared with the Services module."""
    from app.models.service import ServiceCategory
    cat = ServiceCategory(
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        sort_order=payload.sort_order or 0,
    )
    db.add(cat)
    await db.commit()
    return success_response(data={"id": str(cat.id)}, message="Category created")


@router.put("/categories/{cat_id}", summary="Update service category [Admin]")
async def update_category(
    cat_id: UUID,
    payload: UpdateCategoryRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.service import ServiceCategory
    cat = (await db.execute(select(ServiceCategory).where(ServiceCategory.id == cat_id))).scalar_one_or_none()
    if not cat:
        raise HTTPException(404, "Category not found")
    for k, v in payload.dict(exclude_none=True).items():
        if hasattr(cat, k):
            setattr(cat, k, v)
    await db.commit()
    return success_response(message="Category updated")


# ══════════════════════════════════════════════════════════════════════════════
# BRANDS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/brands", summary="List brands [Staff]")
async def list_brands(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    """
    Returns brands from ApplianceBrand (the single shared brand table).
    Brands are managed from the Appliances section; this endpoint exposes
    them for spare-part item creation in inventory.
    """
    from app.models.appliance import ApplianceBrand
    brands = (await db.execute(
        select(ApplianceBrand)
        .where(ApplianceBrand.is_active == True)
        .order_by(ApplianceBrand.name)
    )).scalars().all()
    return success_response(data=[{"id": str(b.id), "name": b.name} for b in brands])


@router.post("/brands", summary="Create brand [Admin]")
async def create_brand(
    payload: CreateBrandRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new ApplianceBrand — shared with the Appliances module."""
    from app.models.appliance import ApplianceBrand
    # Check for duplicate
    existing = (await db.execute(
        select(ApplianceBrand).where(ApplianceBrand.name == payload.name)
    )).scalar_one_or_none()
    if existing:
        return success_response(data={"id": str(existing.id)}, message="Brand already exists")
    b = ApplianceBrand(name=payload.name)
    db.add(b); await db.commit()
    return success_response(data={"id": str(b.id)}, message="Brand created")


# ══════════════════════════════════════════════════════════════════════════════
# WAREHOUSES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/warehouses", summary="List warehouses [Staff]")
async def list_warehouses(current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.inventory import Warehouse
    wh = (await db.execute(select(Warehouse).where(Warehouse.is_active == True).order_by(Warehouse.name))).scalars().all()
    return success_response(data=[{
        "id": str(w.id), "name": w.name, "code": w.code,
        "city": w.city, "address": w.address, "phone": w.phone, "is_default": w.is_default
    } for w in wh])


@router.post("/warehouses", summary="Create warehouse [Admin]")
async def create_warehouse(
    payload: CreateWarehouseRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import Warehouse
    w = Warehouse(**{k: v for k, v in payload.model_dump().items() if k not in ("city_id",)})
    if payload.city_id:
        w.city_id = UUID(payload.city_id)
    db.add(w); await db.commit()
    return success_response(data={"id": str(w.id)}, message="Warehouse created")


@router.put("/warehouses/{wh_id}", summary="Update warehouse [Admin]")
async def update_warehouse(
    wh_id: UUID,
    payload: UpdateWarehouseRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import Warehouse
    wh = (await db.execute(select(Warehouse).where(Warehouse.id == wh_id))).scalar_one_or_none()
    if not wh: raise HTTPException(404, "Warehouse not found")
    for k, v in payload.dict(exclude_none=True).items(): setattr(wh, k, v)
    await db.commit()
    return success_response(message="Warehouse updated")


@router.get("/warehouses/{wh_id}/stock", summary="Stock levels for a warehouse [Staff]")
async def warehouse_stock(
    wh_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=200),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import WarehouseStock, InventoryItem
    q = (
        select(WarehouseStock, InventoryItem)
        .join(InventoryItem, WarehouseStock.item_id == InventoryItem.id)
        .where(WarehouseStock.warehouse_id == wh_id, InventoryItem.is_active == True)
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).all()
    return success_response(data={
        "items": [{
            "item_id": str(ws.item_id), "item_name": item.name, "sku": item.sku,
            "unit": item.unit, "quantity": ws.quantity, "reserved_qty": ws.reserved_qty,
            "available": ws.quantity - ws.reserved_qty,
            "is_low_stock": ws.quantity <= item.min_stock_level
        } for ws, item in rows],
        "total": total, "page": page, "per_page": per_page
    })


# ══════════════════════════════════════════════════════════════════════════════
# ITEMS — CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("", summary="List inventory items [Staff]")
async def list_items(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, le=100),
    search: Optional[str] = None,
    category_id: Optional[str] = None,   # filter by service_category id
    low_stock: bool = Query(False),
    is_consumable: Optional[bool] = None,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import InventoryItem
    q = select(InventoryItem).where(InventoryItem.is_active == True)
    if search:
        like = f"%{search}%"
        q = q.where(or_(InventoryItem.name.ilike(like), InventoryItem.sku.ilike(like), InventoryItem.barcode.ilike(like)))
    if category_id:
        # Filter via many-to-many: items linked to this service_category
        cat_item_ids = (await db.execute(
            text("SELECT item_id FROM item_service_categories WHERE category_id = :cid"),
            {"cid": UUID(category_id)}
        )).scalars().all()
        if not cat_item_ids:
            return success_response(data={"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0})
        q = q.where(InventoryItem.id.in_(cat_item_ids))
    if low_stock:
        q = q.where(InventoryItem.current_stock <= InventoryItem.min_stock_level)
    if is_consumable is not None:
        q = q.where(InventoryItem.is_consumable == is_consumable)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(InventoryItem.name).offset((page-1)*per_page).limit(per_page))).scalars().all()

    # Fetch all categories for these items in one query
    item_ids = [i.id for i in items]
    cat_map = await _get_item_categories(item_ids, db)

    # Fetch brand names in one query
    brand_ids = [i.brand_id for i in items if i.brand_id]
    brand_map: dict = {}
    if brand_ids:
        from app.models.appliance import ApplianceBrand
        brand_rows = (await db.execute(
            select(ApplianceBrand).where(ApplianceBrand.id.in_(brand_ids))
        )).scalars().all()
        brand_map = {str(b.id): b.name for b in brand_rows}

    return success_response(data={
        "items": [_item_row(i, cat_map.get(str(i.id), []), brand_map.get(str(i.brand_id))) for i in items],
        "total": total, "page": page, "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    })


@router.post("", summary="Create inventory item [Admin]")
async def create_item(
    payload: CreateItemRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import InventoryItem
    d = payload.model_dump()
    category_ids = d.pop("category_ids", None) or []
    cat_id   = d.pop("category_id", None) or None   # legacy single
    brand_id = d.pop("brand_id",   None) or None
    # Normalise empty strings → None for unique-constrained fields
    d["sku"]     = d.get("sku")     or None
    d["barcode"] = d.get("barcode") or None
    # Merge legacy single into list
    if cat_id and str(cat_id) not in [str(x) for x in category_ids]:
        category_ids.insert(0, cat_id)
    item = InventoryItem(**d)
    if brand_id: item.brand_id = UUID(brand_id)
    db.add(item)
    await db.flush()  # get item.id before linking categories
    await _sync_item_categories(item.id, category_ids, db)
    await db.commit()
    cats = await _get_item_categories([item.id], db)
    return success_response(data=_item_row(item, cats.get(str(item.id), [])), message="Item created")


@router.get("/low-stock", summary="Items below reorder level [Admin]")
async def low_stock_items(
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import InventoryItem
    items = (await db.execute(
        select(InventoryItem).where(
            InventoryItem.is_active == True,
            InventoryItem.current_stock <= InventoryItem.min_stock_level
        ).order_by(InventoryItem.current_stock)
    )).scalars().all()
    cat_map = await _get_item_categories([i.id for i in items], db)
    return success_response(data=[_item_row(i, cat_map.get(str(i.id), [])) for i in items])



# ══════════════════════════════════════════════════════════════════════════════
# READ-ONLY AGGREGATE / LISTING ROUTES (must come BEFORE /{item_id} catch-all)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/movements", summary="Full stock movement ledger [Admin]")
async def stock_movements(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, le=100),
    item_id: Optional[str] = None,
    technician_id: Optional[str] = None,
    booking_id: Optional[str] = None,
    movement_type: Optional[str] = None,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, InventoryItem
    q = select(StockMovement).order_by(StockMovement.created_at.desc())
    if item_id: q = q.where(StockMovement.item_id == UUID(item_id))
    if technician_id: q = q.where(StockMovement.technician_id == UUID(technician_id))
    if booking_id: q = q.where(StockMovement.booking_id == UUID(booking_id))
    if movement_type: q = q.where(StockMovement.movement_type == movement_type)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    movements = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()

    # Enrich with item names
    item_ids = list({m.item_id for m in movements})
    item_map = {}
    if item_ids:
        items = (await db.execute(select(InventoryItem).where(InventoryItem.id.in_(item_ids)))).scalars().all()
        item_map = {str(i.id): i.name for i in items}

    rows = []
    for m in movements:
        r = _movement_row(m)
        r["item_name"] = item_map.get(str(m.item_id))
        rows.append(r)

    return success_response(data={"items": rows, "total": total, "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page})


# ══════════════════════════════════════════════════════════════════════════════
# STOCK SUMMARY (aggregated)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stock", summary="Aggregated stock levels [Staff]")
async def stock_summary(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=200),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import InventoryItem
    q = select(InventoryItem).where(InventoryItem.is_active == True)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.order_by(InventoryItem.name).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{
            "item_id": str(i.id), "item_name": i.name, "sku": i.sku, "unit": i.unit,
            "current_stock": i.current_stock or 0,
            "reserved_stock": i.reserved_stock or 0,
            "available_stock": max(0, (i.current_stock or 0) - (i.reserved_stock or 0)),
            "min_stock_level": i.min_stock_level or 0,
            "is_low_stock": (i.current_stock or 0) <= (i.min_stock_level or 0)
        } for i in items],
        "total": total, "page": page, "per_page": per_page
    })


# ══════════════════════════════════════════════════════════════════════════════
# REORDER RULES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/reorder-rules", summary="List reorder rules [Admin]")
async def list_reorder_rules(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.inventory import ReorderRule, InventoryItem
    rows = (await db.execute(
        select(ReorderRule, InventoryItem)
        .join(InventoryItem, ReorderRule.item_id == InventoryItem.id)
        .where(ReorderRule.is_active == True)
    )).all()
    return success_response(data=[{
        "id": str(r.id), "item_id": str(r.item_id), "item_name": item.name,
        "reorder_level": r.reorder_level, "reorder_qty": r.reorder_qty,
        "current_stock": item.current_stock or 0,
        "needs_reorder": (item.current_stock or 0) <= r.reorder_level
    } for r, item in rows])


@router.get("/challans", summary="List transfer challans [Admin]")
async def list_challans(
    page: int = Query(1, ge=1), per_page: int = Query(20, le=100),
    status: Optional[str] = Query(None),
    warehouse_id: Optional[str] = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import TransferChallan, Warehouse
    q = select(TransferChallan).where(TransferChallan.is_active == True)
    if status:       q = q.where(TransferChallan.status == status)
    if warehouse_id: q = q.where(
        (TransferChallan.from_warehouse_id == UUID(warehouse_id)) |
        (TransferChallan.to_warehouse_id   == UUID(warehouse_id))
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    challans = (await db.execute(q.order_by(TransferChallan.created_at.desc())
        .offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{
            "id": str(c.id), "challan_no": c.challan_no, "status": c.status,
            "total_qty": c.total_qty, "total_value": c.total_value,
            "from_warehouse_id": str(c.from_warehouse_id) if c.from_warehouse_id else None,
            "to_warehouse_id":   str(c.to_warehouse_id)   if c.to_warehouse_id   else None,
            "to_technician_id":  str(c.to_technician_id)  if c.to_technician_id  else None,
            "reference_no": c.reference_no, "notes": c.notes,
            "created_at": c.created_at.isoformat(),
            "items": json.loads(c.items_json or "[]"),
        } for c in challans],
        "total": total, "page": page, "per_page": per_page,
    })


@router.get("/direct-sales", summary="List direct sales [Admin]")
async def list_direct_sales(
    page: int = Query(1, ge=1), per_page: int = Query(20, le=100),
    current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import DirectSale
    q = select(DirectSale).where(DirectSale.is_active == True)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    sales = (await db.execute(q.order_by(DirectSale.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{
            "id": str(s.id), "sale_no": s.sale_no,
            "customer_name": s.customer_name, "customer_mobile": s.customer_mobile,
            "subtotal": s.subtotal, "gst_amount": s.gst_amount, "total_amount": s.total_amount,
            "payment_method": s.payment_method, "payment_status": s.payment_status,
            "created_at": s.created_at.isoformat(),
            "items": json.loads(s.items_json or "[]"),
        } for s in sales],
        "total": total, "page": page, "per_page": per_page,
    })


# ══════════════════════════════════════════════════════════════════════════════
# BOOKING PART CONSUMPTION
# ══════════════════════════════════════════════════════════════════════════════

class BookingConsumeRequest(BaseModel):
    booking_id:    str
    warehouse_id:  Optional[str] = None
    technician_id: Optional[str] = None
    items:         List[dict]   # [{item_id, quantity, unit_price?, notes?}]



class PurchaseOrderItem(BaseModel):
    item_id:   str
    quantity:  int
    unit_cost: float = 0.0


class PurchaseOrderRequest(BaseModel):
    warehouse_id:      str
    vendor_id:         Optional[str] = None
    vendor_name:       Optional[str] = None
    vendor_invoice_no: Optional[str] = None
    items:             List[PurchaseOrderItem]
    payment_method:    str = "CASH"
    notes:             Optional[str] = None
    update_cost_price: bool = False

# ── Purchase Order routes (must be before /{item_id} catch-all) ──────────────

@router.get("/purchase-orders", summary="List purchase orders [Admin]")
async def list_purchase_orders(
    page: int = 1, per_page: int = 20,
    warehouse_id: Optional[str] = None,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import text as _text
    offset = (page - 1) * per_page
    where = "WHERE po.is_active = true"
    params: dict = {"limit": per_page, "offset": offset}
    if warehouse_id:
        where += " AND po.warehouse_id = :wh_id"
        params["wh_id"] = warehouse_id

    sql = _text(f"""
        SELECT po.id, po.po_number, po.vendor_id, po.vendor_name, po.vendor_invoice_no,
               po.warehouse_id, w.name as warehouse_name,
               po.items_json, po.subtotal, po.tax_amount, po.total_amount,
               po.payment_method, po.payment_status, po.status, po.notes,
               po.received_at, po.created_at
        FROM purchase_orders po
        LEFT JOIN warehouses w ON w.id = po.warehouse_id
        {where}
        ORDER BY po.created_at DESC
        LIMIT :limit OFFSET :offset
    """)
    count_sql = _text(f"SELECT COUNT(*) FROM purchase_orders po {where}")

    result = await db.execute(sql, params)
    rows = result.mappings().all()
    count_r = await db.execute(count_sql, {k:v for k,v in params.items() if k not in ("limit","offset")})
    total = count_r.scalar() or 0

    items_out = []
    for r in rows:
        try:
            items_list = _json.loads(r["items_json"]) if r["items_json"] else []
        except Exception:
            items_list = []
        items_out.append({
            "id": str(r["id"]),
            "po_number": r["po_number"],
            "vendor_id": str(r["vendor_id"]) if r["vendor_id"] else None,
            "vendor_name": r["vendor_name"],
            "vendor_invoice_no": r["vendor_invoice_no"],
            "warehouse_id": str(r["warehouse_id"]),
            "warehouse_name": r["warehouse_name"],
            "items": items_list,
            "subtotal": r["subtotal"],
            "tax_amount": r["tax_amount"],
            "total_amount": r["total_amount"],
            "payment_method": r["payment_method"],
            "payment_status": r["payment_status"],
            "status": r["status"],
            "notes": r["notes"],
            "received_at": r["received_at"].isoformat() if r["received_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return success_response(data={
        "items": items_out, "total": total,
        "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page)
    })


@router.post("/purchase-orders", summary="Create purchase order & receive stock [Admin]")
async def create_purchase_order(
    payload: PurchaseOrderRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import text as _text
    from app.models.inventory import (
        InventoryItem, WarehouseStock, StockMovement, MovementType
    )

    wh_id = UUID(payload.warehouse_id)

    # ── Generate PO number ─────────────────────────────────────────────
    ts = _dt.utcnow().strftime("%y%m%d%H%M")
    r = await db.execute(_text("SELECT COUNT(*) FROM purchase_orders"))
    seq = (r.scalar() or 0) + 1
    po_number = f"PO-{ts}-{seq:04d}"

    # ── Build items with cost info, update stock ────────────────────────
    enriched_items = []
    subtotal = 0.0

    for line in payload.items:
        item = await _get_item(UUID(line.item_id), db)
        qty = line.quantity
        unit_cost = line.unit_cost or item.cost_price or 0.0

        # Update warehouse stock
        ws = await _get_or_create_wh_stock(wh_id, item.id, db)
        ws.quantity += qty

        # Update central stock counter
        old_stock = item.current_stock or 0
        item.current_stock = old_stock + qty

        # Weighted average cost (only when caller opts in)
        # Formula: new_avg = (old_stock * old_cost + new_qty * new_cost) / (old_stock + new_qty)
        if payload.update_cost_price and unit_cost and item.current_stock > 0:
            old_cost = item.cost_price or 0.0
            item.cost_price = round(
                (old_stock * old_cost + qty * unit_cost) / item.current_stock, 4
            )
        # selling_price and mrp are NEVER changed by a purchase — they are set on the item master

        # Stock movement ledger
        mv = StockMovement(
            item_id=item.id,
            movement_type=MovementType.PURCHASE.value,
            quantity=qty,
            to_warehouse_id=wh_id,
            unit_cost=unit_cost,
            reason=f"Purchase Order {po_number}",
            notes=payload.notes,
            reference_no=po_number,
            performed_by=UUID(current_user["user_id"]),
        )
        db.add(mv)

        line_total = qty * unit_cost
        subtotal += line_total
        enriched_items.append({
            "item_id":   str(item.id),
            "item_name": item.name,
            "sku":       item.sku or "",
            "unit":      item.unit,
            "quantity":  qty,
            "unit_cost": unit_cost,
            "total_cost": line_total,
        })

    # Rough GST estimate (avg 18%)
    tax_amount = round(subtotal * 0.18, 2)
    total_amount = subtotal + tax_amount

    # ── Insert purchase order record ────────────────────────────────────
    from sqlalchemy import insert as _insert
    from sqlalchemy import Table, MetaData

    await db.execute(_text("""
        INSERT INTO purchase_orders
          (po_number, vendor_id, vendor_name, vendor_invoice_no,
           warehouse_id, items_json, subtotal, tax_amount, total_amount,
           payment_method, status, notes, received_at, created_by)
        VALUES
          (:po_no, :vendor_id, :vendor_name, :vendor_invoice_no,
           :wh_id, :items_json, :subtotal, :tax_amount, :total_amount,
           :pay_method, 'RECEIVED', :notes, NOW(), :created_by)
    """), {
        "po_no": po_number,
        "vendor_id": UUID(payload.vendor_id) if payload.vendor_id else None,
        "vendor_name": payload.vendor_name or None,
        "vendor_invoice_no": payload.vendor_invoice_no or None,
        "wh_id": wh_id,
        "items_json": _json.dumps(enriched_items),
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "pay_method": payload.payment_method,
        "notes": payload.notes,
        "created_by": UUID(current_user["user_id"]),
    })

    await db.commit()

    return success_response(data={
        "po_number": po_number,
        "total_amount": total_amount,
        "items_received": len(enriched_items),
    }, message=f"Purchase Order {po_number} created. Stock updated in warehouse.")


@router.get("/purchase-orders/{po_id}", summary="Purchase order detail [Admin]")
async def get_purchase_order(
    po_id: str,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import text as _text
    r = await db.execute(_text("""
        SELECT po.*, w.name as warehouse_name
        FROM purchase_orders po
        LEFT JOIN warehouses w ON w.id = po.warehouse_id
        WHERE po.id = :id AND po.is_active = true
    """), {"id": UUID(po_id)})
    row = r.mappings().first()
    if not row:
        raise HTTPException(404, "Purchase order not found")
    try:
        items = _json.loads(row["items_json"]) if row["items_json"] else []
    except Exception:
        items = []
    data = dict(row)
    data["items"] = items
    data["id"] = str(data["id"])
    data["warehouse_id"] = str(data["warehouse_id"])
    if data.get("vendor_id"): data["vendor_id"] = str(data["vendor_id"])
    if data.get("created_by"): data["created_by"] = str(data["created_by"])
    if data.get("created_at"): data["created_at"] = data["created_at"].isoformat()
    if data.get("received_at"): data["received_at"] = data["received_at"].isoformat()
    return success_response(data=data)


@router.get("/item-warehouse-stock/{item_id}", summary="Get stock of an item across all warehouses [Staff]")
async def item_warehouse_stock(
    item_id: UUID,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import WarehouseStock, Warehouse
    rows = (await db.execute(
        select(WarehouseStock, Warehouse)
        .join(Warehouse, Warehouse.id == WarehouseStock.warehouse_id)
        .where(WarehouseStock.item_id == item_id, WarehouseStock.quantity > 0)
    )).all()
    return success_response(data=[{
        "warehouse_id": str(ws.warehouse_id),
        "warehouse_name": wh.name,
        "quantity": ws.quantity,
        "reserved_qty": ws.reserved_qty or 0,
        "available": ws.quantity - (ws.reserved_qty or 0),
    } for ws, wh in rows])


@router.get("/{item_id}", summary="Item detail [Staff]")
async def get_item(item_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    item = await _get_item(item_id, db)
    cats = await _get_item_categories([item.id], db)
    return success_response(data=_item_row(item, cats.get(str(item.id), [])))


@router.put("/{item_id}", summary="Update item [Admin]")
async def update_item(
    item_id: UUID,
    payload: UpdateItemRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    item = await _get_item(item_id, db)
    d = payload.dict(exclude_none=True)
    # Extract category_ids for many-to-many sync
    category_ids = d.pop("category_ids", None)
    cat_id = d.pop("category_id", None)
    if category_ids is not None:
        # Merge legacy single
        if cat_id and str(cat_id) not in [str(x) for x in category_ids]:
            category_ids.insert(0, cat_id)
        await _sync_item_categories(item.id, category_ids, db)
    elif cat_id is not None:
        await _sync_item_categories(item.id, [cat_id] if cat_id else [], db)
    for k in ("brand_id",):
        if k in d:
            d[k] = UUID(d[k]) if d[k] else None
    # Normalise empty strings → None for unique-constrained fields
    for unique_field in ("sku", "barcode"):
        if unique_field in d:
            d[unique_field] = d[unique_field] or None
    for k, v in d.items():
        if hasattr(item, k): setattr(item, k, v)
    await db.commit()
    cats = await _get_item_categories([item.id], db)
    return success_response(data=_item_row(item, cats.get(str(item.id), [])), message="Item updated")


@router.delete("/{item_id}", summary="Deactivate item [Admin]")
async def deactivate_item(item_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    item = await _get_item(item_id, db)
    item.is_active = False
    await db.commit()
    return success_response(message="Item deactivated")


# ══════════════════════════════════════════════════════════════════════════════
# STOCK MOVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/opening", summary="Set opening stock for an item in a warehouse [Admin]")
async def opening_stock(
    payload: StockOpeningRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType
    item = await _get_item(UUID(payload.item_id), db)
    ws = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item.id, db)
    ws.quantity += payload.quantity
    item.current_stock = (item.current_stock or 0) + payload.quantity
    mv = StockMovement(
        item_id=item.id, movement_type=MovementType.OPENING.value,
        quantity=payload.quantity, to_warehouse_id=UUID(payload.warehouse_id),
        unit_cost=payload.unit_cost, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv)
    await db.commit()
    return success_response(data={"current_stock": item.current_stock}, message="Opening stock set")


@router.post("/adjust", summary="Adjust stock (+/-) [Admin]")
async def adjust_stock(
    payload: StockAdjustmentRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType
    item = await _get_item(UUID(payload.item_id), db)
    ws = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item.id, db)
    new_wh_qty = ws.quantity + payload.quantity
    if new_wh_qty < 0:
        raise HTTPException(400, f"Adjustment would result in negative warehouse stock ({new_wh_qty})")
    ws.quantity = new_wh_qty
    item.current_stock = max(0, (item.current_stock or 0) + payload.quantity)
    mv = StockMovement(
        item_id=item.id, movement_type=MovementType.ADJUSTMENT.value,
        quantity=payload.quantity,
        to_warehouse_id=UUID(payload.warehouse_id) if payload.quantity > 0 else None,
        from_warehouse_id=UUID(payload.warehouse_id) if payload.quantity < 0 else None,
        reason=payload.reason, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv); await db.commit()
    return success_response(data={"current_stock": item.current_stock, "warehouse_qty": ws.quantity}, message="Stock adjusted")


@router.post("/transfer", summary="Transfer stock between warehouses [Admin]")
async def transfer_stock(
    payload: StockTransferRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType
    if payload.from_warehouse_id == payload.to_warehouse_id:
        raise HTTPException(400, "Source and destination warehouse must differ")
    item = await _get_item(UUID(payload.item_id), db)
    from_wh = await _get_or_create_wh_stock(UUID(payload.from_warehouse_id), item.id, db)
    if from_wh.quantity - from_wh.reserved_qty < payload.quantity:
        raise HTTPException(400, f"Insufficient available stock in source warehouse (available: {from_wh.quantity - from_wh.reserved_qty})")
    to_wh = await _get_or_create_wh_stock(UUID(payload.to_warehouse_id), item.id, db)
    from_wh.quantity -= payload.quantity
    to_wh.quantity += payload.quantity
    mv = StockMovement(
        item_id=item.id, movement_type=MovementType.TRANSFER_OUT.value,
        quantity=-payload.quantity,
        from_warehouse_id=UUID(payload.from_warehouse_id),
        to_warehouse_id=UUID(payload.to_warehouse_id),
        reference_no=payload.reference_no, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv); await db.commit()
    return success_response(message="Stock transferred")


@router.post("/assign-tech", summary="Assign stock from warehouse to technician [Admin]")
async def assign_to_technician(
    payload: AssignToTechRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType, TechnicianStockLog, TechnicianStockStatus
    item = await _get_item(UUID(payload.item_id), db)
    wh_stock = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item.id, db)
    available = wh_stock.quantity - (wh_stock.reserved_qty or 0)
    if available < payload.quantity:
        raise HTTPException(400, f"Insufficient stock in selected warehouse. Available in this warehouse: {available} {item.unit}. Total item stock: {item.current_stock or 0}")
    wh_stock.quantity -= payload.quantity
    item.current_stock = max(0, (item.current_stock or 0) - payload.quantity)
    item.reserved_stock = (item.reserved_stock or 0) + payload.quantity

    tech_stock = await _get_or_create_tech_stock(UUID(payload.technician_id), item.id, db)
    tech_stock.quantity += payload.quantity
    tech_stock.assigned_qty = (tech_stock.assigned_qty or 0) + payload.quantity

    # Ledger
    mv = StockMovement(
        item_id=item.id, movement_type=MovementType.ASSIGNMENT.value,
        quantity=-payload.quantity,
        from_warehouse_id=UUID(payload.warehouse_id),
        technician_id=UUID(payload.technician_id),
        booking_id=UUID(payload.booking_id) if payload.booking_id else None,
        notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    log = TechnicianStockLog(
        technician_id=UUID(payload.technician_id), item_id=item.id,
        booking_id=UUID(payload.booking_id) if payload.booking_id else None,
        warehouse_id=UUID(payload.warehouse_id),
        status=TechnicianStockStatus.ASSIGNED.value,
        quantity=payload.quantity, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv); db.add(log); await db.commit()
    return success_response(data={
        "technician_qty": tech_stock.quantity,
        "warehouse_qty": wh_stock.quantity
    }, message=f"Assigned {payload.quantity} {item.unit} of '{item.name}' to technician")


@router.post("/return-tech", summary="Return stock from technician to warehouse [Admin]")
async def return_from_technician(
    payload: ReturnFromTechRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType, TechnicianStockLog, TechnicianStockStatus
    item = await _get_item(UUID(payload.item_id), db)
    tech_stock = await _get_or_create_tech_stock(UUID(payload.technician_id), item.id, db)
    if tech_stock.quantity < payload.quantity:
        raise HTTPException(400, f"Technician only has {tech_stock.quantity} units")
    tech_stock.quantity -= payload.quantity
    tech_stock.returned_qty = (tech_stock.returned_qty or 0) + payload.quantity

    status = TechnicianStockStatus.RETURNED
    if payload.condition == "DAMAGED":
        status = TechnicianStockStatus.DAMAGED
    elif payload.condition == "LOST":
        status = TechnicianStockStatus.LOST

    if payload.condition == "GOOD":
        wh_stock = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item.id, db)
        wh_stock.quantity += payload.quantity
        item.current_stock = (item.current_stock or 0) + payload.quantity

    item.reserved_stock = max(0, (item.reserved_stock or 0) - payload.quantity)
    mv_type = MovementType.RETURN_IN if payload.condition == "GOOD" else MovementType.DAMAGE
    mv = StockMovement(
        item_id=item.id, movement_type=mv_type.value,
        quantity=payload.quantity if payload.condition == "GOOD" else -payload.quantity,
        to_warehouse_id=UUID(payload.warehouse_id) if payload.condition == "GOOD" else None,
        technician_id=UUID(payload.technician_id),
        notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    log = TechnicianStockLog(
        technician_id=UUID(payload.technician_id), item_id=item.id,
        warehouse_id=UUID(payload.warehouse_id),
        status=status, quantity=payload.quantity, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv); db.add(log); await db.commit()
    return success_response(data={"technician_qty": tech_stock.quantity}, message="Stock returned")


@router.post("/consume", summary="Mark parts consumed in a booking [Admin/Technician]")
async def consume_stock(
    payload: ConsumeStockRequest,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType, TechnicianStockLog, TechnicianStockStatus
    results = []
    for entry in payload.items:
        item_id = UUID(entry["item_id"])
        qty = int(entry["quantity"])
        item = await _get_item(item_id, db)
        tech_stock = await _get_or_create_tech_stock(UUID(payload.technician_id), item_id, db)
        if tech_stock.quantity < qty:
            raise HTTPException(400, f"Technician has only {tech_stock.quantity} of '{item.name}'")
        tech_stock.quantity -= qty
        tech_stock.consumed_qty = (tech_stock.consumed_qty or 0) + qty
        item.reserved_stock = max(0, (item.reserved_stock or 0) - qty)
        mv = StockMovement(
            item_id=item_id, movement_type=MovementType.CONSUMPTION.value,
            quantity=-qty, technician_id=UUID(payload.technician_id),
            booking_id=UUID(payload.booking_id),
            unit_cost=entry.get("unit_cost"), notes=payload.notes,
            performed_by=UUID(current_user["user_id"])
        )
        log = TechnicianStockLog(
            technician_id=UUID(payload.technician_id), item_id=item_id,
            booking_id=UUID(payload.booking_id),
            status=TechnicianStockStatus.CONSUMED.value, quantity=qty, notes=payload.notes,
            performed_by=UUID(current_user["user_id"])
        )
        db.add(mv); db.add(log)
        results.append({"item_id": str(item_id), "item_name": item.name, "consumed": qty, "remaining": tech_stock.quantity})
    await db.commit()
    return success_response(data={"consumed": results}, message="Stock consumption recorded")


@router.post("/damage", summary="Write off damaged stock [Admin]")
async def damage_stock(
    payload: DamageRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import StockMovement, MovementType
    item = await _get_item(UUID(payload.item_id), db)
    if payload.warehouse_id:
        wh_stock = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item.id, db)
        if wh_stock.quantity < payload.quantity:
            raise HTTPException(400, "Insufficient warehouse stock to write off")
        wh_stock.quantity -= payload.quantity
    if payload.technician_id:
        ts = await _get_or_create_tech_stock(UUID(payload.technician_id), item.id, db)
        if ts.quantity < payload.quantity:
            raise HTTPException(400, "Technician does not have enough stock")
        ts.quantity -= payload.quantity
    item.current_stock = max(0, (item.current_stock or 0) - payload.quantity)
    mv = StockMovement(
        item_id=item.id, movement_type=MovementType.DAMAGE.value,
        quantity=-payload.quantity,
        from_warehouse_id=UUID(payload.warehouse_id) if payload.warehouse_id else None,
        technician_id=UUID(payload.technician_id) if payload.technician_id else None,
        reason=payload.reason, notes=payload.notes,
        performed_by=UUID(current_user["user_id"])
    )
    db.add(mv); await db.commit()
    return success_response(message="Damage recorded")


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICIAN STOCK VIEW
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/technician/{technician_id}", summary="Technician's current stock [Staff]")
async def technician_stock(
    technician_id: UUID,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import TechnicianStock, InventoryItem
    rows = (await db.execute(
        select(TechnicianStock, InventoryItem)
        .join(InventoryItem, TechnicianStock.item_id == InventoryItem.id)
        .where(TechnicianStock.technician_id == technician_id, TechnicianStock.quantity > 0)
        .order_by(InventoryItem.name)
    )).all()
    return success_response(data=[{
        "item_id": str(ts.item_id), "item_name": item.name, "sku": item.sku,
        "unit": item.unit, "quantity": ts.quantity,
        "assigned_qty": ts.assigned_qty, "consumed_qty": ts.consumed_qty,
        "returned_qty": ts.returned_qty
    } for ts, item in rows])


@router.get("/technician/{technician_id}/history", summary="Technician stock event history [Staff]")
async def technician_stock_history(
    technician_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, le=100),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import TechnicianStockLog, InventoryItem
    q = (
        select(TechnicianStockLog, InventoryItem)
        .join(InventoryItem, TechnicianStockLog.item_id == InventoryItem.id)
        .where(TechnicianStockLog.technician_id == technician_id)
        .order_by(TechnicianStockLog.created_at.desc())
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).all()
    return success_response(data={
        "items": [{
            "id": str(log.id), "item_id": str(log.item_id), "item_name": item.name,
            "sku": item.sku, "unit": item.unit,
            "status": log.status.value if hasattr(log.status, 'value') else log.status,
            "quantity": log.quantity, "booking_id": str(log.booking_id) if log.booking_id else None,
            "notes": log.notes, "created_at": log.created_at.isoformat() if log.created_at else None
        } for log, item in rows],
        "total": total, "page": page, "per_page": per_page
    })


# ══════════════════════════════════════════════════════════════════════════════
# LEDGER / MOVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/reorder-rules", summary="Create/update reorder rule [Admin]")
async def upsert_reorder_rule(
    payload: ReorderRuleRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import ReorderRule
    existing = (await db.execute(
        select(ReorderRule).where(ReorderRule.item_id == UUID(payload.item_id))
    )).scalar_one_or_none()
    if existing:
        existing.reorder_level = payload.reorder_level
        existing.reorder_qty = payload.reorder_qty
        if payload.warehouse_id: existing.warehouse_id = UUID(payload.warehouse_id)
        if payload.preferred_vendor_id: existing.preferred_vendor_id = UUID(payload.preferred_vendor_id)
    else:
        rule = ReorderRule(
            item_id=UUID(payload.item_id),
            reorder_level=payload.reorder_level,
            reorder_qty=payload.reorder_qty,
            warehouse_id=UUID(payload.warehouse_id) if payload.warehouse_id else None,
            preferred_vendor_id=UUID(payload.preferred_vendor_id) if payload.preferred_vendor_id else None
        )
        db.add(rule)
    await db.commit()
    return success_response(message="Reorder rule saved")


# ══════════════════════════════════════════════════════════════════════════════
# TRANSFER CHALLANS
# ══════════════════════════════════════════════════════════════════════════════

class ChallanTransferRequest(BaseModel):
    """Transfer stock between warehouses and generate a delivery challan."""
    from_warehouse_id: str
    to_warehouse_id:   Optional[str] = None   # null if going to technician
    to_technician_id:  Optional[str] = None   # null if going to another warehouse
    items: List[dict]   # [{item_id, quantity, notes?}]
    reference_no: Optional[str] = None
    notes: Optional[str] = None

class ChallanReceiveRequest(BaseModel):
    challan_id: str
    notes:      Optional[str] = None

import json, random, string as _string

def _gen_challan_no() -> str:
    suffix = ''.join(random.choices(_string.digits, k=8))
    return f"CHN{suffix}"

def _gen_sale_no() -> str:
    suffix = ''.join(random.choices(_string.digits, k=8))
    return f"SAL{suffix}"

@router.post("/challans", summary="Create transfer challan (warehouse→warehouse or warehouse→technician) [Admin]")
async def create_challan(
    payload: ChallanTransferRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import (
        TransferChallan, InventoryItem, WarehouseStock,
        StockMovement, MovementType, TechnicianStock, TechnicianStockLog, TechnicianStockStatus
    )
    if not payload.to_warehouse_id and not payload.to_technician_id:
        raise HTTPException(400, "Must specify to_warehouse_id or to_technician_id")
    if payload.to_warehouse_id == payload.from_warehouse_id:
        raise HTTPException(400, "Source and destination warehouse must differ")

    challan_items = []
    total_qty, total_value = 0, 0.0

    for line in payload.items:
        item_id = UUID(line["item_id"])
        qty     = int(line["quantity"])
        item    = (await db.execute(
            select(InventoryItem).where(InventoryItem.id == item_id, InventoryItem.is_active == True)
        )).scalar_one_or_none()
        if not item:
            raise HTTPException(404, f"Item {item_id} not found")

        # Check source warehouse stock
        from_wh = await _get_or_create_wh_stock(UUID(payload.from_warehouse_id), item_id, db)
        available = from_wh.quantity - from_wh.reserved_qty
        if available < qty:
            raise HTTPException(400, f"Insufficient stock for '{item.name}' (available: {available})")

        # Deduct from source
        from_wh.quantity -= qty
        item.current_stock = max(0, (item.current_stock or 0) - qty)

        # Credit destination
        if payload.to_warehouse_id:
            to_wh = await _get_or_create_wh_stock(UUID(payload.to_warehouse_id), item_id, db)
            to_wh.quantity += qty
            mv_type = MovementType.TRANSFER_OUT
        else:
            # Assign to technician
            tech_stock = await _get_or_create_tech_stock(UUID(payload.to_technician_id), item_id, db)
            tech_stock.quantity += qty
            tech_stock.assigned_qty = (tech_stock.assigned_qty or 0) + qty
            log = TechnicianStockLog(
                technician_id=UUID(payload.to_technician_id), item_id=item_id,
                warehouse_id=UUID(payload.from_warehouse_id),
                status=TechnicianStockStatus.ASSIGNED.value, quantity=qty,
                notes=line.get("notes"), performed_by=UUID(current_user["user_id"])
            )
            db.add(log)
            mv_type = MovementType.ASSIGNMENT

        # Ledger entry per item
        mv = StockMovement(
            item_id=item_id, movement_type=mv_type.value, quantity=-qty,
            from_warehouse_id=UUID(payload.from_warehouse_id),
            to_warehouse_id=UUID(payload.to_warehouse_id) if payload.to_warehouse_id else None,
            technician_id=UUID(payload.to_technician_id) if payload.to_technician_id else None,
            reference_no=payload.reference_no, notes=payload.notes or line.get("notes"),
            unit_cost=item.cost_price, performed_by=UUID(current_user["user_id"])
        )
        db.add(mv)

        challan_items.append({
            "item_id": str(item_id), "item_name": item.name, "sku": item.sku or "",
            "unit": item.unit, "quantity": qty,
            "unit_cost": item.cost_price, "line_total": round(item.cost_price * qty, 2)
        })
        total_qty   += qty
        total_value += item.cost_price * qty

    # Create challan document
    challan = TransferChallan(
        challan_no        = _gen_challan_no(),
        from_warehouse_id = UUID(payload.from_warehouse_id),
        to_warehouse_id   = UUID(payload.to_warehouse_id) if payload.to_warehouse_id else None,
        to_technician_id  = UUID(payload.to_technician_id) if payload.to_technician_id else None,
        items_json        = json.dumps(challan_items),
        total_qty         = total_qty,
        total_value       = round(total_value, 2),
        status            = "IN_TRANSIT",
        reference_no      = payload.reference_no,
        notes             = payload.notes,
        created_by        = UUID(current_user["user_id"]),
    )
    db.add(challan)
    await db.commit()

    return success_response(data={
        "challan_id":  str(challan.id),
        "challan_no":  challan.challan_no,
        "status":      challan.status,
        "total_qty":   total_qty,
        "total_value": round(total_value, 2),
        "items":       challan_items,
    }, message=f"Challan {challan.challan_no} created — {total_qty} item(s) dispatched")


@router.get("/challans/{challan_id}", summary="Challan details [Admin]")
async def get_challan(challan_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.inventory import TransferChallan
    c = (await db.execute(select(TransferChallan).where(TransferChallan.id == challan_id))).scalar_one_or_none()
    if not c: raise HTTPException(404, "Challan not found")
    return success_response(data={
        "id": str(c.id), "challan_no": c.challan_no, "status": c.status,
        "total_qty": c.total_qty, "total_value": c.total_value,
        "from_warehouse_id": str(c.from_warehouse_id) if c.from_warehouse_id else None,
        "to_warehouse_id":   str(c.to_warehouse_id)   if c.to_warehouse_id   else None,
        "to_technician_id":  str(c.to_technician_id)  if c.to_technician_id  else None,
        "reference_no": c.reference_no, "notes": c.notes,
        "dispatched_at": c.dispatched_at.isoformat() if c.dispatched_at else None,
        "received_at": c.received_at.isoformat() if c.received_at else None,
        "created_at": c.created_at.isoformat(),
        "items": json.loads(c.items_json or "[]"),
    })


@router.post("/challans/{challan_id}/receive", summary="Mark challan as received [Admin]")
async def receive_challan(
    challan_id: UUID,
    payload: ChallanReceiveRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import TransferChallan
    from datetime import datetime as dt
    c = (await db.execute(select(TransferChallan).where(TransferChallan.id == challan_id))).scalar_one_or_none()
    if not c: raise HTTPException(404, "Challan not found")
    if c.status == "DELIVERED": raise HTTPException(400, "Challan already received")
    c.status      = "DELIVERED"
    c.received_at = dt.utcnow()
    c.received_by = UUID(current_user["user_id"])
    if payload.notes: c.notes = payload.notes
    await db.commit()
    return success_response(data={"challan_no": c.challan_no, "status": c.status}, message="Challan marked as received")


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT SALE (admin sells spare part directly)
# ══════════════════════════════════════════════════════════════════════════════

class DirectSaleRequest(BaseModel):
    warehouse_id:    str
    customer_id:     Optional[str] = None
    customer_name:   Optional[str] = None
    customer_mobile: Optional[str] = None
    booking_id:      Optional[str] = None
    items:           List[dict]   # [{item_id, quantity, unit_price, notes?}]
    payment_method:  str = "CASH"
    notes:           Optional[str] = None

@router.post("/direct-sale", summary="Direct sale of spare part to customer [Admin]")
async def direct_sale(
    payload: DirectSaleRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import DirectSale, InventoryItem, WarehouseStock, StockMovement, MovementType
    sale_items, subtotal, gst_amount = [], 0.0, 0.0

    for line in payload.items:
        item_id    = UUID(line["item_id"])
        qty        = int(line["quantity"])
        unit_price = float(line.get("unit_price", 0))
        item = (await db.execute(
            select(InventoryItem).where(InventoryItem.id == item_id, InventoryItem.is_active == True)
        )).scalar_one_or_none()
        if not item: raise HTTPException(404, f"Item {item_id} not found")

        wh_stock = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item_id, db)
        if wh_stock.quantity < qty:
            raise HTTPException(400, f"Insufficient stock for '{item.name}' (available: {wh_stock.quantity})")

        wh_stock.quantity     -= qty
        item.current_stock     = max(0, (item.current_stock or 0) - qty)
        line_gst               = round(unit_price * qty * (item.gst_percent / 100), 2)
        line_total             = round(unit_price * qty, 2)

        mv = StockMovement(
            item_id=item_id, movement_type=MovementType.CONSUMPTION.value,
            quantity=-qty, from_warehouse_id=UUID(payload.warehouse_id),
            booking_id=UUID(payload.booking_id) if payload.booking_id else None,
            unit_cost=item.cost_price, notes=f"Direct sale {_gen_sale_no()}",
            performed_by=UUID(current_user["user_id"])
        )
        db.add(mv)
        sale_items.append({
            "item_id": str(item_id), "item_name": item.name, "sku": item.sku or "",
            "unit": item.unit, "quantity": qty, "unit_price": unit_price,
            "gst_percent": item.gst_percent, "gst_amount": line_gst, "total": line_total + line_gst
        })
        subtotal   += line_total
        gst_amount += line_gst

    sale = DirectSale(
        sale_no         = _gen_sale_no(),
        warehouse_id    = UUID(payload.warehouse_id),
        customer_id     = UUID(payload.customer_id) if payload.customer_id else None,
        customer_name   = payload.customer_name,
        customer_mobile = payload.customer_mobile,
        booking_id      = UUID(payload.booking_id) if payload.booking_id else None,
        items_json      = json.dumps(sale_items),
        subtotal        = round(subtotal, 2),
        gst_amount      = round(gst_amount, 2),
        total_amount    = round(subtotal + gst_amount, 2),
        payment_method  = payload.payment_method,
        payment_status  = "PAID",
        notes           = payload.notes,
        sold_by         = UUID(current_user["user_id"]),
    )
    db.add(sale)
    await db.commit()
    return success_response(data={
        "sale_id": str(sale.id), "sale_no": sale.sale_no,
        "subtotal": sale.subtotal, "gst_amount": sale.gst_amount,
        "total_amount": sale.total_amount, "payment_method": sale.payment_method,
        "items": sale_items,
    }, message=f"Sale {sale.sale_no} recorded — ₹{sale.total_amount}")


@router.post("/booking-consume", summary="Record parts consumed in a booking [Admin/Technician]")
async def booking_consume(
    payload: BookingConsumeRequest,
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.inventory import (
        BookingPartUsage, InventoryItem, WarehouseStock,
        TechnicianStock, StockMovement, MovementType
    )
    results = []
    for line in payload.items:
        item_id    = UUID(line["item_id"])
        qty        = int(line["quantity"])
        unit_price = float(line.get("unit_price", 0))
        item = (await db.execute(
            select(InventoryItem).where(InventoryItem.id == item_id, InventoryItem.is_active == True)
        )).scalar_one_or_none()
        if not item: raise HTTPException(404, f"Item {item_id} not found")

        # Deduct from technician stock if provided, else from warehouse
        if payload.technician_id:
            tech_stock = await _get_or_create_tech_stock(UUID(payload.technician_id), item_id, db)
            if tech_stock.quantity < qty:
                raise HTTPException(400, f"Technician doesn't have enough '{item.name}' (has: {tech_stock.quantity})")
            tech_stock.quantity       -= qty
            tech_stock.consumed_qty    = (tech_stock.consumed_qty or 0) + qty
        elif payload.warehouse_id:
            wh_stock = await _get_or_create_wh_stock(UUID(payload.warehouse_id), item_id, db)
            if wh_stock.quantity < qty:
                raise HTTPException(400, f"Insufficient warehouse stock for '{item.name}'")
            wh_stock.quantity  -= qty
            item.current_stock  = max(0, (item.current_stock or 0) - qty)

        # Ledger
        mv = StockMovement(
            item_id=item_id, movement_type=MovementType.CONSUMPTION.value, quantity=-qty,
            from_warehouse_id=UUID(payload.warehouse_id) if payload.warehouse_id else None,
            technician_id=UUID(payload.technician_id) if payload.technician_id else None,
            booking_id=UUID(payload.booking_id),
            unit_cost=item.cost_price, notes=line.get("notes"),
            performed_by=UUID(current_user["user_id"])
        )
        db.add(mv)

        # Booking part usage log
        bpu = BookingPartUsage(
            booking_id    = UUID(payload.booking_id),
            item_id       = item_id,
            technician_id = UUID(payload.technician_id) if payload.technician_id else None,
            warehouse_id  = UUID(payload.warehouse_id) if payload.warehouse_id else None,
            quantity      = qty,
            unit_cost     = item.cost_price,
            unit_price    = unit_price or item.selling_price,
            total_amount  = round((unit_price or item.selling_price) * qty, 2),
            notes         = line.get("notes"),
            created_by    = UUID(current_user["user_id"]),
        )
        db.add(bpu)
        results.append({"item_id": str(item_id), "item_name": item.name, "qty": qty,
                         "unit_price": bpu.unit_price, "total": bpu.total_amount})

    await db.commit()
    return success_response(data={"booking_id": payload.booking_id, "parts": results},
                             message=f"{len(results)} part(s) consumed in booking")


@router.get("/booking-parts/{booking_id}", summary="Parts consumed in a booking [Staff]")
async def booking_parts(booking_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.inventory import BookingPartUsage, InventoryItem
    rows = (await db.execute(
        select(BookingPartUsage, InventoryItem.name.label("item_name"), InventoryItem.sku.label("sku"), InventoryItem.unit.label("unit"))
        .join(InventoryItem, InventoryItem.id == BookingPartUsage.item_id)
        .where(BookingPartUsage.booking_id == booking_id, BookingPartUsage.is_active == True)
    )).all()
    return success_response(data=[{
        "id": str(r.BookingPartUsage.id), "item_id": str(r.BookingPartUsage.item_id),
        "item_name": r.item_name, "sku": r.sku, "unit": r.unit,
        "quantity": r.BookingPartUsage.quantity, "unit_price": r.BookingPartUsage.unit_price,
        "total_amount": r.BookingPartUsage.total_amount, "notes": r.BookingPartUsage.notes,
        "created_at": r.BookingPartUsage.created_at.isoformat()
    } for r in rows])


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASE ORDERS
# ══════════════════════════════════════════════════════════════════════════════

import json as _json
from datetime import datetime as _dt

