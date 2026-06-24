from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly
from app.utils.response import success_response

router = APIRouter()

class CreateFranchiseRequest(BaseModel):
    name: str; owner_user_id: Optional[str] = None; city: Optional[str] = None
    state: Optional[str] = None; address: Optional[str] = None; phone: Optional[str] = None
    email: Optional[str] = None; commission_rate: float = 0.0

@router.get("", summary="List franchises [Admin]")
async def list_franchises(page: int = Query(1, ge=1), per_page: int = Query(20),
                          current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.franchise import Franchise
    q = select(Franchise).where(Franchise.is_active == True)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(f.id), "name": f.name, "city": f.city,
                                              "state": f.state, "commission_rate": f.commission_rate,
                                              "created_at": f.created_at.isoformat()} for f in items], "total": total})

@router.post("", summary="Create franchise [Admin]")
async def create_franchise(payload: CreateFranchiseRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.franchise import Franchise
    data = payload.dict()
    if data.get("owner_user_id"): data["owner_user_id"] = UUID(data["owner_user_id"])
    f = Franchise(**data); db.add(f); await db.commit()
    return success_response(data={"id": str(f.id)}, message="Franchise created")

@router.get("/{franchise_id}", summary="Franchise detail [Admin]")
async def get_franchise(franchise_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.franchise import Franchise
    f = (await db.execute(select(Franchise).where(Franchise.id == franchise_id))).scalar_one_or_none()
    if not f: raise HTTPException(404, "Franchise not found")
    return success_response(data={"id": str(f.id), "name": f.name, "city": f.city, "state": f.state,
                                   "address": f.address, "phone": f.phone, "email": f.email,
                                   "commission_rate": f.commission_rate})
