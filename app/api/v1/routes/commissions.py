from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff
from app.utils.response import success_response

router = APIRouter()

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class CreateRuleRequest(BaseModel):
    name: str; role: str; commission_type: str; rate: float; applies_to: str = "BOOKING"

class ApproveCommissionRequest(BaseModel):
    notes: Optional[str] = None

class GroupRuleIn(BaseModel):
    service_id:      str
    domain_id:       Optional[str] = None
    commission_type: str = "PERCENTAGE"
    rate:            float = 0.0

class CreateGroupRequest(BaseModel):
    name:        str
    description: Optional[str] = None
    rules:       List[GroupRuleIn] = []

class UpdateGroupRequest(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    is_active:   Optional[bool] = None
    rules:       Optional[List[GroupRuleIn]] = None

class PartRuleIn(BaseModel):
    part_name_match:    Optional[str] = None
    part_source_filter: Optional[str] = None
    commission_type:    str = "PERCENTAGE"
    rate:               float = 0.0


# ── Commission Rules ──────────────────────────────────────────────────────────
@router.get("/rules", summary="Commission rules [Admin]")
async def list_rules(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionRule
    rules = (await db.execute(select(CommissionRule).where(CommissionRule.is_active == True))).scalars().all()
    return success_response(data=[{"id": str(r.id), "name": r.name, "role": r.role,
                                    "commission_type": r.commission_type, "rate": r.rate} for r in rules])

@router.post("/rules", summary="Create commission rule [Admin]")
async def create_rule(payload: CreateRuleRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionRule
    rule = CommissionRule(**payload.dict())
    db.add(rule); await db.commit()
    return success_response(data={"id": str(rule.id)}, message="Rule created")


# ── Commissions Ledger ────────────────────────────────────────────────────────
@router.get("", summary="Commissions list [Admin]")
async def list_commissions(
    page:           int           = Query(1, ge=1),
    per_page:       int           = Query(20, ge=1, le=100),
    status:         Optional[str] = Query(None),
    technician_id:  Optional[str] = Query(None),
    item_type:      Optional[str] = Query(None),
    search:         Optional[str] = Query(None, description="Search by technician name or code"),
    current_user:   dict          = Depends(AdminOnly),
    db:             AsyncSession  = Depends(get_db),
):
    from app.models.commission import Commission
    from app.models.technician import Technician
    from sqlalchemy import or_

    # If searching by technician name/code, resolve tech IDs first
    search_tech_ids = None
    if search:
        techs = (await db.execute(
            select(Technician).where(
                or_(
                    Technician.name.ilike(f"%{search}%"),
                    Technician.technician_code.ilike(f"%{search}%"),
                )
            )
        )).scalars().all()
        search_tech_ids = [t.id for t in techs]
        if not search_tech_ids:
            # No techs match — return empty with zeros
            return success_response(data={
                "items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0,
                "summary": {"total_amount": 0, "total_count": 0, "pending_amount": 0,
                            "approved_amount": 0, "paid_amount": 0,
                            "pending_count": 0, "approved_count": 0, "paid_count": 0},
            })

    q = select(Commission)
    if status:           q = q.where(Commission.status == status)
    if technician_id:    q = q.where(Commission.technician_id == UUID(technician_id))
    if item_type:        q = q.where(Commission.item_type == item_type)
    if search_tech_ids:  q = q.where(Commission.technician_id.in_(search_tech_ids))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    items = (await db.execute(
        q.order_by(Commission.created_at.desc())
         .offset((page - 1) * per_page)
         .limit(per_page)
    )).scalars().all()

    # Enrich with technician names
    tech_ids = list({c.technician_id for c in items if c.technician_id})
    tech_map = {}
    if tech_ids:
        rows = (await db.execute(select(Technician).where(Technician.id.in_(tech_ids)))).scalars().all()
        tech_map = {str(t.id): t for t in rows}

    # Aggregate summary
    agg = (await db.execute(
        select(
            func.coalesce(func.sum(Commission.commission_amount), 0).label("total_amount"),
            func.count(Commission.id).label("total_count"),
            func.coalesce(func.sum(Commission.commission_amount).filter(Commission.status == "PENDING"), 0).label("pending_amount"),
            func.coalesce(func.sum(Commission.commission_amount).filter(Commission.status == "APPROVED"), 0).label("approved_amount"),
            func.coalesce(func.sum(Commission.commission_amount).filter(Commission.status == "PAID"), 0).label("paid_amount"),
            func.count(Commission.id).filter(Commission.status == "PENDING").label("pending_count"),
            func.count(Commission.id).filter(Commission.status == "APPROVED").label("approved_count"),
            func.count(Commission.id).filter(Commission.status == "PAID").label("paid_count"),
        )
    )).one()

    return success_response(data={
        "items": [{
            "id":                str(c.id),
            "technician_id":     str(c.technician_id),
            "technician_name":   tech_map.get(str(c.technician_id), {}).name if tech_map.get(str(c.technician_id)) else None,
            "technician_code":   tech_map.get(str(c.technician_id), {}).technician_code if tech_map.get(str(c.technician_id)) else None,
            "booking_id":        str(c.booking_id) if c.booking_id else None,
            "base_amount":       round(c.base_amount or 0, 2),
            "commission_amount": round(c.commission_amount or 0, 2),
            "status":            c.status,
            "item_type":         c.item_type,
            "item_name":         c.item_name,
            "item_quantity":     c.item_quantity,
            "part_source":       c.part_source,
            "payout_date":       c.payout_date.isoformat() if c.payout_date else None,
            "notes":             c.notes,
            "created_at":        c.created_at.isoformat(),
        } for c in items],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "summary": {
            "total_amount":    round(float(agg.total_amount), 2),
            "total_count":     agg.total_count,
            "pending_amount":  round(float(agg.pending_amount), 2),
            "approved_amount": round(float(agg.approved_amount), 2),
            "paid_amount":     round(float(agg.paid_amount), 2),
            "pending_count":   agg.pending_count,
            "approved_count":  agg.approved_count,
            "paid_count":      agg.paid_count,
        },
    })


@router.post("/{commission_id}/approve", summary="Approve commission [Admin]")
async def approve_commission(commission_id: UUID, payload: ApproveCommissionRequest,
                             current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import Commission
    c = (await db.execute(select(Commission).where(Commission.id == commission_id))).scalar_one_or_none()
    if not c: raise HTTPException(404, "Commission not found")
    c.status = "APPROVED"; c.notes = payload.notes
    await db.commit()
    return success_response(message="Commission approved")


@router.post("/{commission_id}/pay", summary="Mark commission paid [Admin]")
async def pay_commission(commission_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import Commission
    from datetime import datetime, timezone
    c = (await db.execute(select(Commission).where(Commission.id == commission_id))).scalar_one_or_none()
    if not c: raise HTTPException(404, "Commission not found")
    if c.status != "APPROVED": raise HTTPException(400, "Commission must be APPROVED before marking PAID")
    c.status = "PAID"; c.payout_date = datetime.now(timezone.utc)
    await db.commit()
    return success_response(message="Commission marked as paid")


@router.post("/bulk-approve", summary="Bulk approve PENDING commissions [Admin]")
async def bulk_approve(
    payload: dict,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import Commission
    ids = payload.get("ids", [])
    if not ids: raise HTTPException(400, "No IDs provided")
    items = (await db.execute(
        select(Commission).where(Commission.id.in_([UUID(i) for i in ids]), Commission.status == "PENDING")
    )).scalars().all()
    for c in items:
        c.status = "APPROVED"
    await db.commit()
    return success_response(data={"updated": len(items)}, message=f"{len(items)} commissions approved")


@router.post("/bulk-pay", summary="Bulk mark APPROVED commissions as PAID [Admin]")
async def bulk_pay(
    payload: dict,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import Commission
    from datetime import datetime, timezone
    ids = payload.get("ids", [])
    if not ids: raise HTTPException(400, "No IDs provided")
    items = (await db.execute(
        select(Commission).where(Commission.id.in_([UUID(i) for i in ids]), Commission.status == "APPROVED")
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for c in items:
        c.status = "PAID"; c.payout_date = now
    await db.commit()
    return success_response(data={"updated": len(items)}, message=f"{len(items)} commissions marked paid")


# ── Commission Groups ─────────────────────────────────────────────────────────
@router.get("/groups", summary="List commission groups [Admin]")
async def list_groups(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule, CommissionGroupAssignment
    groups = (await db.execute(
        select(CommissionGroup).where(CommissionGroup.is_active == True).order_by(CommissionGroup.created_at.desc())
    )).scalars().all()
    result = []
    for g in groups:
        rules = (await db.execute(select(CommissionGroupRule).where(CommissionGroupRule.group_id == g.id))).scalars().all()
        tech_count = (await db.execute(
            select(func.count(CommissionGroupAssignment.id)).where(CommissionGroupAssignment.group_id == g.id)
        )).scalar_one()
        result.append({
            "id": str(g.id), "name": g.name, "description": g.description,
            "is_active": g.is_active, "technician_count": tech_count,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "rules": [{"id": str(r.id), "service_id": str(r.service_id),
                       "domain_id": str(r.domain_id) if r.domain_id else None,
                       "commission_type": r.commission_type, "rate": r.rate} for r in rules]
        })
    return success_response(data=result)


@router.post("/groups", summary="Create commission group [Admin]")
async def create_group(payload: CreateGroupRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule
    g = CommissionGroup(name=payload.name, description=payload.description)
    db.add(g); await db.flush()
    for r in payload.rules:
        db.add(CommissionGroupRule(
            group_id=g.id, service_id=UUID(r.service_id),
            domain_id=UUID(r.domain_id) if r.domain_id else None,
            commission_type=r.commission_type, rate=r.rate,
        ))
    await db.commit()
    return success_response(data={"id": str(g.id)}, message="Commission group created")


@router.get("/groups/{group_id}", summary="Get commission group [Admin]")
async def get_group(group_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule, CommissionGroupAssignment
    from app.models.technician import Technician
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    rules = (await db.execute(select(CommissionGroupRule).where(CommissionGroupRule.group_id == group_id))).scalars().all()
    assignments = (await db.execute(select(CommissionGroupAssignment).where(CommissionGroupAssignment.group_id == group_id))).scalars().all()
    tech_ids = [a.technician_id for a in assignments]
    techs = []
    if tech_ids:
        t_rows = (await db.execute(select(Technician).where(Technician.id.in_(tech_ids)))).scalars().all()
        techs = [{"id": str(t.id), "name": t.name, "mobile": t.mobile, "technician_code": t.technician_code} for t in t_rows]
    return success_response(data={
        "id": str(g.id), "name": g.name, "description": g.description, "is_active": g.is_active,
        "rules": [{"id": str(r.id), "service_id": str(r.service_id),
                   "domain_id": str(r.domain_id) if r.domain_id else None,
                   "commission_type": r.commission_type, "rate": r.rate} for r in rules],
        "technicians": techs,
    })


@router.put("/groups/{group_id}", summary="Update commission group [Admin]")
async def update_group(group_id: UUID, payload: UpdateGroupRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    if payload.name        is not None: g.name        = payload.name
    if payload.description is not None: g.description = payload.description
    if payload.is_active   is not None: g.is_active   = payload.is_active
    if payload.rules is not None:
        await db.execute(CommissionGroupRule.__table__.delete().where(CommissionGroupRule.group_id == group_id))
        for r in payload.rules:
            db.add(CommissionGroupRule(
                group_id=group_id, service_id=UUID(r.service_id),
                domain_id=UUID(r.domain_id) if r.domain_id else None,
                commission_type=r.commission_type, rate=r.rate,
            ))
    await db.commit()
    return success_response(message="Group updated")


@router.delete("/groups/{group_id}", summary="Delete commission group [Admin]")
async def delete_group(group_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    g.is_active = False; await db.commit()
    return success_response(message="Group deactivated")


@router.post("/groups/{group_id}/assign", summary="Assign technician to group [Admin]")
async def assign_technician(group_id: UUID, payload: dict, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupAssignment
    tech_id = payload.get("technician_id")
    if not tech_id: raise HTTPException(400, "technician_id required")
    existing = (await db.execute(
        select(CommissionGroupAssignment).where(
            CommissionGroupAssignment.technician_id == UUID(tech_id),
            CommissionGroupAssignment.group_id == group_id
        )
    )).scalar_one_or_none()
    if existing: return success_response(message="Already assigned")
    db.add(CommissionGroupAssignment(technician_id=UUID(tech_id), group_id=group_id))
    await db.commit()
    return success_response(message="Technician assigned to group")


@router.delete("/groups/{group_id}/assign/{technician_id}", summary="Remove technician from group [Admin]")
async def remove_assignment(group_id: UUID, technician_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroupAssignment
    row = (await db.execute(
        select(CommissionGroupAssignment).where(
            CommissionGroupAssignment.technician_id == technician_id,
            CommissionGroupAssignment.group_id == group_id
        )
    )).scalar_one_or_none()
    if not row: raise HTTPException(404, "Assignment not found")
    await db.delete(row); await db.commit()
    return success_response(message="Technician removed from group")


@router.get("/groups-for-technician/{technician_id}", summary="Groups assigned to a technician [Admin]")
async def groups_for_technician(technician_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupAssignment, CommissionGroupRule
    assignments = (await db.execute(
        select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == technician_id)
    )).scalars().all()
    group_ids = [a.group_id for a in assignments]
    if not group_ids: return success_response(data=[])
    groups = (await db.execute(select(CommissionGroup).where(CommissionGroup.id.in_(group_ids)))).scalars().all()
    result = []
    for g in groups:
        rules = (await db.execute(select(CommissionGroupRule).where(CommissionGroupRule.group_id == g.id))).scalars().all()
        result.append({
            "id": str(g.id), "name": g.name,
            "rules": [{"service_id": str(r.service_id), "commission_type": r.commission_type, "rate": r.rate} for r in rules]
        })
    return success_response(data=result)


# ── Part Commission Rules ─────────────────────────────────────────────────────
@router.get("/groups/{group_id}/part-rules", summary="List part commission rules for a group [Admin]")
async def list_part_rules(group_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroupPartRule
    rules = (await db.execute(select(CommissionGroupPartRule).where(CommissionGroupPartRule.group_id == group_id))).scalars().all()
    return success_response(data=[{
        "id": str(r.id), "group_id": str(r.group_id),
        "part_name_match": r.part_name_match, "part_source_filter": r.part_source_filter,
        "commission_type": r.commission_type, "rate": r.rate,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rules])


@router.post("/groups/{group_id}/part-rules", summary="Add part commission rule to group [Admin]")
async def add_part_rule(group_id: UUID, payload: PartRuleIn, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroupPartRule, CommissionGroup
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    rule = CommissionGroupPartRule(
        group_id=group_id, part_name_match=payload.part_name_match or None,
        part_source_filter=payload.part_source_filter or None,
        commission_type=payload.commission_type, rate=payload.rate,
    )
    db.add(rule); await db.commit()
    return success_response(data={"id": str(rule.id)}, message="Part rule added")


@router.put("/groups/{group_id}/part-rules/{rule_id}", summary="Update part commission rule [Admin]")
async def update_part_rule(group_id: UUID, rule_id: UUID, payload: PartRuleIn, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroupPartRule
    rule = (await db.execute(
        select(CommissionGroupPartRule).where(
            CommissionGroupPartRule.id == rule_id, CommissionGroupPartRule.group_id == group_id
        )
    )).scalar_one_or_none()
    if not rule: raise HTTPException(404, "Part rule not found")
    rule.part_name_match = payload.part_name_match or None
    rule.part_source_filter = payload.part_source_filter or None
    rule.commission_type = payload.commission_type
    rule.rate = payload.rate
    await db.commit()
    return success_response(message="Part rule updated")


@router.delete("/groups/{group_id}/part-rules/{rule_id}", summary="Delete part commission rule [Admin]")
async def delete_part_rule(group_id: UUID, rule_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroupPartRule
    rule = (await db.execute(
        select(CommissionGroupPartRule).where(
            CommissionGroupPartRule.id == rule_id, CommissionGroupPartRule.group_id == group_id
        )
    )).scalar_one_or_none()
    if not rule: raise HTTPException(404, "Part rule not found")
    await db.delete(rule); await db.commit()
    return success_response(message="Part rule deleted")
