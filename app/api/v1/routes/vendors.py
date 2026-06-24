from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff
from app.utils.response import success_response

router = APIRouter()

class CreateVendorRequest(BaseModel):
    name: str; contact_person: Optional[str] = None; mobile: Optional[str] = None
    email: Optional[str] = None; gstin: Optional[str] = None; address: Optional[str] = None

class UpdateVendorRequest(BaseModel):
    name: Optional[str] = None; contact_person: Optional[str] = None; mobile: Optional[str] = None
    email: Optional[str] = None; gstin: Optional[str] = None; address: Optional[str] = None

@router.get("", summary="List vendors [Staff]")
async def list_vendors(page: int = Query(1, ge=1), per_page: int = Query(20), current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.vendor import Vendor
    q = select(Vendor).where(Vendor.is_active == True)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    vendors = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(v.id), "name": v.name, "contact_person": v.contact_person, "mobile": v.mobile, "email": v.email, "gstin": v.gstin} for v in vendors], "total": total})

@router.post("", summary="Create vendor [Admin]")
async def create_vendor(payload: CreateVendorRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.vendor import Vendor
    vendor = Vendor(**payload.model_dump()); db.add(vendor); await db.commit()
    return success_response(data={"id": str(vendor.id), "name": vendor.name}, message="Vendor created")

@router.get("/{vendor_id}", summary="Vendor details [Staff]")
async def get_vendor(vendor_id: UUID, current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.vendor import Vendor
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id, Vendor.is_active == True))).scalar_one_or_none()
    if not vendor: raise HTTPException(status_code=404, detail="Vendor not found")
    return success_response(data={"id": str(vendor.id), "name": vendor.name, "contact_person": vendor.contact_person, "mobile": vendor.mobile, "email": vendor.email, "gstin": vendor.gstin, "address": vendor.address})

@router.put("/{vendor_id}", summary="Update vendor [Admin]")
async def update_vendor(vendor_id: UUID, payload: UpdateVendorRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.vendor import Vendor
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if not vendor: raise HTTPException(status_code=404, detail="Vendor not found")
    for f, v in payload.model_dump(exclude_none=True).items(): setattr(vendor, f, v)
    await db.commit()
    return success_response(message="Vendor updated")

@router.get("/{vendor_id}/transactions", summary="Vendor transactions [Staff]")
async def vendor_transactions(vendor_id: UUID, page: int = Query(1, ge=1), per_page: int = Query(20), current_user: dict = Depends(AnyStaff), db: AsyncSession = Depends(get_db)):
    from app.models.vendor import VendorTransaction
    txns = (await db.execute(select(VendorTransaction).where(VendorTransaction.vendor_id == vendor_id).order_by(VendorTransaction.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data=[{"id": str(t.id), "amount": t.amount, "type": t.type, "notes": t.notes, "created_at": t.created_at.isoformat()} for t in txns])
