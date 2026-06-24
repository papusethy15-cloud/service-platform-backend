from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class CreatePlanRequest(BaseModel):
    name: str; plan_type: str = "GOLD"; price: float; duration_months: int = 12
    visit_count: int; description: Optional[str] = None; appliance_types: Optional[str] = None

class PurchaseAMCRequest(BaseModel):
    customer_id: str; plan_id: str; appliance_id: Optional[str] = None; start_date: Optional[str] = None

class AMCVisitRequest(BaseModel):
    amc_id: str; scheduled_date: str; technician_id: Optional[str] = None; notes: Optional[str] = None

class RenewAMCRequest(BaseModel):
    amc_id: str; payment_amount: Optional[float] = None

@router.get("/plans", summary="List AMC plans [Public]")
async def list_plans(db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCPlan
    items = (await db.execute(select(AMCPlan).where(AMCPlan.is_active == True).order_by(AMCPlan.price))).scalars().all()
    return success_response(data=[{
        "id": str(p.id), "name": p.name, "plan_type": p.plan_type,
        "price": p.price, "duration_months": p.duration_months,
        "visit_count": p.visit_count, "description": p.description
    } for p in items])

@router.post("/plans", summary="Create AMC plan [Admin]")
async def create_plan(payload: CreatePlanRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCPlan
    plan = AMCPlan(**payload.model_dump()); db.add(plan); await db.commit()
    return success_response(data={"id": str(plan.id), "name": plan.name}, message="AMC plan created")

@router.put("/plans/{plan_id}", summary="Update AMC plan [Admin]")
async def update_plan(plan_id: UUID, payload: CreatePlanRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCPlan
    plan = (await db.execute(select(AMCPlan).where(AMCPlan.id == plan_id))).scalar_one_or_none()
    if not plan: raise HTTPException(status_code=404, detail="Plan not found")
    for f, v in payload.model_dump(exclude_none=True).items(): setattr(plan, f, v)
    await db.commit()
    return success_response(message="Plan updated")

@router.post("/purchase", summary="Purchase AMC [Admin/CCO]")
async def purchase_amc(payload: PurchaseAMCRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCPlan, AMCSubscription
    from datetime import timedelta
    plan = (await db.execute(select(AMCPlan).where(AMCPlan.id == UUID(payload.plan_id)))).scalar_one_or_none()
    if not plan: raise HTTPException(status_code=404, detail="Plan not found")
    start = datetime.fromisoformat(payload.start_date) if payload.start_date else datetime.utcnow()
    end = start.replace(month=start.month + plan.duration_months if start.month + plan.duration_months <= 12
                        else ((start.month + plan.duration_months) % 12), year=start.year + (start.month + plan.duration_months - 1) // 12)
    sub = AMCSubscription(customer_id=UUID(payload.customer_id), plan_id=UUID(payload.plan_id),
                          start_date=start, end_date=end, visits_remaining=plan.visit_count,
                          amount_paid=plan.price)
    db.add(sub); await db.commit()
    return success_response(data={"id": str(sub.id), "start_date": start.isoformat(), "end_date": end.isoformat()}, message="AMC purchased")

@router.get("/customer/{customer_id}", summary="Customer AMC subscriptions [Staff]")
async def customer_amc(customer_id: UUID, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCSubscription
    items = (await db.execute(select(AMCSubscription).where(AMCSubscription.customer_id == customer_id))).scalars().all()
    return success_response(data=[{"id": str(s.id), "plan_id": str(s.plan_id),
                                    "start_date": s.start_date.isoformat(), "end_date": s.end_date.isoformat(),
                                    "visits_remaining": s.visits_remaining, "status": s.status} for s in items])

@router.post("/visit", summary="Schedule AMC visit [Admin/CCO]")
async def schedule_visit(payload: AMCVisitRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCVisit, AMCSubscription
    sub = (await db.execute(select(AMCSubscription).where(AMCSubscription.id == UUID(payload.amc_id)))).scalar_one_or_none()
    if not sub: raise HTTPException(status_code=404, detail="AMC subscription not found")
    if sub.visits_remaining <= 0: raise HTTPException(status_code=400, detail="No visits remaining")
    visit = AMCVisit(amc_id=UUID(payload.amc_id), scheduled_date=datetime.fromisoformat(payload.scheduled_date),
                     technician_id=UUID(payload.technician_id) if payload.technician_id else None, notes=payload.notes)
    sub.visits_remaining -= 1
    db.add(visit); await db.commit()
    return success_response(data={"id": str(visit.id)}, message="AMC visit scheduled")

@router.get("/renewals", summary="AMC renewals due [Admin/CCO]")
async def renewals_due(days: int = Query(30), current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCSubscription
    from datetime import timedelta
    cutoff = datetime.utcnow() + timedelta(days=days)
    items = (await db.execute(select(AMCSubscription).where(AMCSubscription.end_date <= cutoff,
                                                             AMCSubscription.is_active == True))).scalars().all()
    return success_response(data=[{"id": str(s.id), "customer_id": str(s.customer_id),
                                    "end_date": s.end_date.isoformat(), "plan_id": str(s.plan_id)} for s in items])

@router.post("/renew", summary="Renew AMC [Admin/CCO]")
async def renew_amc(payload: RenewAMCRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.amc import AMCSubscription
    sub = (await db.execute(select(AMCSubscription).where(AMCSubscription.id == UUID(payload.amc_id)))).scalar_one_or_none()
    if not sub: raise HTTPException(status_code=404, detail="AMC not found")
    from dateutil.relativedelta import relativedelta
    plan = (await db.execute(select(__import__("app.models.amc", fromlist=["AMCPlan"]).AMCPlan).where(__import__("app.models.amc", fromlist=["AMCPlan"]).AMCPlan.id == sub.plan_id))).scalar_one_or_none()
    if plan: sub.end_date = sub.end_date + relativedelta(months=plan.duration_months)
    sub.visits_remaining = plan.visit_count if plan else sub.visits_remaining
    await db.commit()
    return success_response(data={"new_end_date": sub.end_date.isoformat()}, message="AMC renewed")
