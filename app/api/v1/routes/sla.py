from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO
from app.utils.response import success_response

router = APIRouter()

class CreateSLAPolicyRequest(BaseModel):
    name: str; description: Optional[str] = None; response_time_minutes: Optional[int] = None
    resolution_time_hours: Optional[int] = None; priority: str = "STANDARD"

@router.get("/policies", summary="SLA policies [Admin]")
async def list_policies(current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.sla import SLAPolicy
    policies = (await db.execute(select(SLAPolicy).where(SLAPolicy.is_active == True))).scalars().all()
    return success_response(data=[{"id": str(p.id), "name": p.name, "priority": p.priority,
                                    "response_time_minutes": p.response_time_minutes,
                                    "resolution_time_hours": p.resolution_time_hours} for p in policies])

@router.post("/policies", summary="Create SLA policy [Admin]")
async def create_policy(payload: CreateSLAPolicyRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.sla import SLAPolicy
    p = SLAPolicy(**payload.dict()); db.add(p); await db.commit()
    return success_response(data={"id": str(p.id)}, message="SLA policy created")

@router.get("/breaches", summary="SLA breaches [Admin]")
async def list_breaches(page: int = Query(1, ge=1), per_page: int = Query(20),
                        current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.sla import SLABreach
    q = select(SLABreach).order_by(SLABreach.breached_at.desc())
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    breaches = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(b.id), "booking_id": str(b.booking_id),
                                              "breach_type": b.breach_type,
                                              "breached_at": b.breached_at.isoformat()} for b in breaches], "total": total})
