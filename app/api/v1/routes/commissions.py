from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyStaff
from app.utils.response import success_response, iso

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
    name:             str
    description:      Optional[str] = None
    is_salary_group:  bool = False
    monthly_salary:   Optional[float] = None
    petrol_amount:    Optional[float] = 0.0
    mobile_recharge:  Optional[float] = 0.0
    bonus_amount:     Optional[float] = 0.0
    hra_amount:       Optional[float] = 0.0
    other_allowances: Optional[float] = 0.0
    salary_notes:     Optional[str] = None
    rules:            List[GroupRuleIn] = []

class UpdateGroupRequest(BaseModel):
    name:             Optional[str] = None
    description:      Optional[str] = None
    is_active:        Optional[bool] = None
    is_salary_group:  Optional[bool] = None
    monthly_salary:   Optional[float] = None
    petrol_amount:    Optional[float] = None
    mobile_recharge:  Optional[float] = None
    bonus_amount:     Optional[float] = None
    hra_amount:       Optional[float] = None
    other_allowances: Optional[float] = None
    salary_notes:     Optional[str] = None
    rules:            Optional[List[GroupRuleIn]] = None

class PartRuleIn(BaseModel):
    part_name_match:    Optional[str] = None
    part_source_filter: Optional[str] = None
    commission_type:    str = "PERCENTAGE"
    rate:               float = 0.0



# ── Service Price Preview (for commission group editor) ───────────────────────
@router.get("/service-price-preview", summary="Get service price structure for commission setting [Admin]")
async def service_price_preview(
    service_id: str = Query(..., description="Service UUID"),
    domain_id:  Optional[str] = Query(None, description="Optional domain UUID to filter city prices"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the base price + all city override prices for a service.
    If domain_id is provided, also returns which cities are linked to that domain
    so the admin knows which city prices apply for technicians under that domain.
    Priority: domain city price > city price > base price.
    """
    from app.models.service import Service
    from app.models.domain import ServiceCityPrice, DomainCity
    from app.models.city import City

    svc = (await db.execute(
        select(Service).where(Service.id == UUID(service_id), Service.is_active == True)
    )).scalar_one_or_none()
    if not svc:
        raise HTTPException(404, "Service not found")

    # All city prices for this service
    city_rows = (await db.execute(
        select(ServiceCityPrice, City.name.label("city_name"), City.state.label("city_state"))
        .join(City, City.id == ServiceCityPrice.city_id)
        .where(ServiceCityPrice.service_id == UUID(service_id), ServiceCityPrice.is_available == True)
        .order_by(City.name)
    )).all()

    # If domain scoped — which cities does this domain serve?
    domain_city_ids: set = set()
    if domain_id:
        dc_rows = (await db.execute(
            select(DomainCity.city_id).where(DomainCity.domain_id == UUID(domain_id))
        )).scalars().all()
        domain_city_ids = {str(cid) for cid in dc_rows}

    city_prices = []
    for row in city_rows:
        cp = row.ServiceCityPrice
        city_id_str = str(cp.city_id)
        in_domain = (len(domain_city_ids) == 0) or (city_id_str in domain_city_ids)
        city_prices.append({
            "city_id":    city_id_str,
            "city_name":  row.city_name,
            "city_state": row.city_state,
            "price":      cp.price,
            "in_domain":  in_domain,   # True if this city is served by the selected domain
        })

    return success_response(data={
        "service_id":   str(svc.id),
        "service_name": svc.name,
        "base_price":   svc.base_price,
        "gst_percent":  svc.gst_percent,
        "city_prices":  city_prices,
        "has_overrides": len(city_prices) > 0,
    })

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
            "base_amount":       int(round(c.base_amount or 0)),
            "commission_amount": int(round(c.commission_amount or 0)),
            "status":            c.status,
            "item_type":         c.item_type,
            "item_name":         c.item_name,
            "item_quantity":     c.item_quantity,
            "part_source":       c.part_source,
            "payout_date":       iso(c.payout_date) if c.payout_date else None,
            "notes":             c.notes,
            "created_at":        iso(c.created_at),
        } for c in items],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "summary": {
            "total_amount":    int(round(float(agg.total_amount))),
            "total_count":     agg.total_count,
            "pending_amount":  int(round(float(agg.pending_amount))),
            "approved_amount": int(round(float(agg.approved_amount))),
            "paid_amount":     int(round(float(agg.paid_amount))),
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
    if c.status != "PENDING": raise HTTPException(400, f"Commission is already {c.status}; only PENDING commissions can be approved")
    c.status = "APPROVED"; c.notes = payload.notes
    await db.commit()
    return success_response(message="Commission approved")


@router.post("/{commission_id}/pay", summary="Mark commission paid [Admin]")
async def pay_commission(commission_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import Commission
    from app.models.wallet import Wallet, WalletTransaction
    from datetime import datetime, timezone
    c = (await db.execute(select(Commission).where(Commission.id == commission_id))).scalar_one_or_none()
    if not c: raise HTTPException(404, "Commission not found")
    if c.status == "PAID": raise HTTPException(400, "Commission is already PAID — wallet was already credited; cannot pay again")
    if c.status != "APPROVED": raise HTTPException(400, "Commission must be APPROVED before marking PAID")

    now = datetime.now(timezone.utc)
    c.status = "PAID"
    c.payout_date = now

    # Credit the technician's wallet — get-or-create so new technicians are never skipped
    wallet = (await db.execute(
        select(Wallet).where(Wallet.technician_id == c.technician_id)
    )).scalar_one_or_none()
    if not wallet:
        from app.models.technician import Technician
        _tech = (await db.execute(select(Technician).where(Technician.id == c.technician_id))).scalar_one_or_none()
        wallet = Wallet(technician_id=c.technician_id,
                        user_id=_tech.user_id if _tech else None,
                        balance=0.0, total_earned=0.0, total_withdrawn=0.0)
        db.add(wallet)
        await db.flush()  # get wallet.id before using it in WalletTransaction

    balance_before = wallet.balance or 0
    wallet.balance = round(balance_before + (c.commission_amount or 0), 2)
    wallet.total_earned = round((wallet.total_earned or 0) + (c.commission_amount or 0), 2)
    db.add(WalletTransaction(
        wallet_id=wallet.id,
        transaction_type="CREDIT",
        amount=c.commission_amount or 0,
        balance_before=balance_before,
        balance_after=wallet.balance,
        description=f"Commission paid: {c.item_name or c.item_type or 'Commission'}",
        reference_id=str(c.booking_id) if c.booking_id else None,
        status="SUCCESS",
    ))

    await db.commit()
    return success_response(data={"new_balance": round(wallet.balance, 2)}, message="Commission marked as paid and wallet credited")


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
    from app.models.wallet import Wallet, WalletTransaction
    now = datetime.now(timezone.utc)

    # Group by technician so we do one wallet lookup per tech
    # get-or-create wallet — never silently skip a technician without a wallet
    tech_wallet_map: dict = {}
    for c in items:
        c.status = "PAID"
        c.payout_date = now
        tid = str(c.technician_id)
        if tid not in tech_wallet_map:
            w = (await db.execute(select(Wallet).where(Wallet.technician_id == c.technician_id))).scalar_one_or_none()
            if not w:
                from app.models.technician import Technician
                _tech = (await db.execute(select(Technician).where(Technician.id == c.technician_id))).scalar_one_or_none()
                w = Wallet(technician_id=c.technician_id,
                           user_id=_tech.user_id if _tech else None,
                           balance=0.0, total_earned=0.0, total_withdrawn=0.0)
                db.add(w)
                await db.flush()  # get w.id for WalletTransaction FK
            tech_wallet_map[tid] = w
        wallet = tech_wallet_map[tid]
        balance_before = wallet.balance or 0
        wallet.balance = round(balance_before + (c.commission_amount or 0), 2)
        wallet.total_earned = round((wallet.total_earned or 0) + (c.commission_amount or 0), 2)
        db.add(WalletTransaction(
            wallet_id=wallet.id,
            transaction_type="CREDIT",
            amount=c.commission_amount or 0,
            balance_before=balance_before,
            balance_after=wallet.balance,
            description=f"Commission paid: {c.item_name or c.item_type or 'Commission'}",
            reference_id=str(c.booking_id) if c.booking_id else None,
            status="SUCCESS",
        ))

    await db.commit()
    return success_response(data={"updated": len(items)}, message=f"{len(items)} commissions marked paid and wallets credited")


# ── Commission Groups ─────────────────────────────────────────────────────────
@router.get("/groups", summary="List commission groups [Admin]")
async def list_groups(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule, CommissionGroupAssignment
    from app.models.service import Service
    groups = (await db.execute(
        select(CommissionGroup).where(CommissionGroup.is_active == True).order_by(CommissionGroup.created_at.desc())
    )).scalars().all()

    # Pre-load ALL service names in one query across all groups
    all_rules_rows = (await db.execute(
        select(CommissionGroupRule).where(CommissionGroupRule.group_id.in_([g.id for g in groups]))
    )).scalars().all()
    all_svc_ids = list({r.service_id for r in all_rules_rows if r.service_id})
    svc_map: dict = {}
    if all_svc_ids:
        svcs = (await db.execute(select(Service).where(Service.id.in_(all_svc_ids)))).scalars().all()
        svc_map = {str(s.id): s for s in svcs}
    rules_by_group: dict = {}
    for r in all_rules_rows:
        rules_by_group.setdefault(str(r.group_id), []).append(r)

    result = []
    for g in groups:
        tech_count = (await db.execute(
            select(func.count(CommissionGroupAssignment.id)).where(CommissionGroupAssignment.group_id == g.id)
        )).scalar_one()
        grules = rules_by_group.get(str(g.id), [])
        result.append({
            "id": str(g.id), "name": g.name, "description": g.description,
            "is_active": g.is_active, "is_salary_group": bool(g.is_salary_group),
            "monthly_salary": g.monthly_salary, "petrol_amount": g.petrol_amount or 0,
            "mobile_recharge": g.mobile_recharge or 0, "bonus_amount": g.bonus_amount or 0,
            "hra_amount": g.hra_amount or 0, "other_allowances": g.other_allowances or 0,
            "salary_notes": g.salary_notes,
            "technician_count": tech_count,
            "created_at": iso(g.created_at) if g.created_at else None,
            "rules": [{
                "id":              str(r.id),
                "service_id":      str(r.service_id),
                "service_name":    svc_map[str(r.service_id)].name if str(r.service_id) in svc_map else str(r.service_id),
                "domain_id":       str(r.domain_id) if r.domain_id else None,
                "commission_type": r.commission_type,
                "rate":            r.rate,
            } for r in grules]
        })
    return success_response(data=result)


@router.post("/groups", summary="Create commission group [Admin]")
async def create_group(payload: CreateGroupRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule
    g = CommissionGroup(
        name=payload.name, description=payload.description,
        is_salary_group=bool(payload.is_salary_group),
        monthly_salary=payload.monthly_salary,
        petrol_amount=payload.petrol_amount or 0,
        mobile_recharge=payload.mobile_recharge or 0,
        bonus_amount=payload.bonus_amount or 0,
        hra_amount=payload.hra_amount or 0,
        other_allowances=payload.other_allowances or 0,
        salary_notes=payload.salary_notes,
    )
    db.add(g); await db.flush()
    # Salary groups don't have service/part commission rules
    rules_to_save = [] if payload.is_salary_group else payload.rules
    for r in rules_to_save:
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
    from app.models.service import Service
    from app.models.domain import Domain
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    rules = (await db.execute(select(CommissionGroupRule).where(CommissionGroupRule.group_id == group_id))).scalars().all()

    # Enrich rules: resolve service names and domain names in one query each
    svc_ids = list({r.service_id for r in rules if r.service_id})
    dom_ids = list({r.domain_id  for r in rules if r.domain_id})
    svc_map: dict = {}
    dom_map: dict = {}
    if svc_ids:
        svcs = (await db.execute(select(Service).where(Service.id.in_(svc_ids)))).scalars().all()
        svc_map = {str(s.id): s for s in svcs}
    if dom_ids:
        doms = (await db.execute(select(Domain).where(Domain.id.in_(dom_ids)))).scalars().all()
        dom_map = {str(d.id): d for d in doms}

    assignments = (await db.execute(select(CommissionGroupAssignment).where(CommissionGroupAssignment.group_id == group_id))).scalars().all()
    tech_ids = [a.technician_id for a in assignments]
    techs = []
    if tech_ids:
        t_rows = (await db.execute(select(Technician).where(Technician.id.in_(tech_ids)))).scalars().all()
        techs = [{"id": str(t.id), "name": t.name, "mobile": t.mobile, "technician_code": t.technician_code} for t in t_rows]

    def _enrich_rule(r):
        svc = svc_map.get(str(r.service_id))
        dom = dom_map.get(str(r.domain_id)) if r.domain_id else None
        return {
            "id":              str(r.id),
            "service_id":      str(r.service_id),
            "service_name":    svc.name       if svc else str(r.service_id),
            "base_price":      svc.base_price if svc else None,
            "domain_id":       str(r.domain_id) if r.domain_id else None,
            "domain_name":     dom.name if dom else None,
            "commission_type": r.commission_type,
            "rate":            r.rate,
        }

    return success_response(data={
        "id": str(g.id), "name": g.name, "description": g.description,
        "is_active": g.is_active, "is_salary_group": bool(g.is_salary_group),
        "monthly_salary": g.monthly_salary, "petrol_amount": g.petrol_amount or 0,
        "mobile_recharge": g.mobile_recharge or 0, "bonus_amount": g.bonus_amount or 0,
        "hra_amount": g.hra_amount or 0, "other_allowances": g.other_allowances or 0,
        "salary_notes": g.salary_notes,
        "rules": [_enrich_rule(r) for r in rules],
        "technicians": techs,
    })


@router.put("/groups/{group_id}", summary="Update commission group [Admin]")
async def update_group(group_id: UUID, payload: UpdateGroupRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.commission import CommissionGroup, CommissionGroupRule
    g = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == group_id))).scalar_one_or_none()
    if not g: raise HTTPException(404, "Group not found")
    if payload.name             is not None: g.name             = payload.name
    if payload.description      is not None: g.description      = payload.description
    if payload.is_active        is not None: g.is_active        = payload.is_active
    if payload.is_salary_group  is not None: g.is_salary_group  = payload.is_salary_group
    if payload.monthly_salary   is not None: g.monthly_salary   = payload.monthly_salary
    if payload.petrol_amount    is not None: g.petrol_amount    = payload.petrol_amount
    if payload.mobile_recharge  is not None: g.mobile_recharge  = payload.mobile_recharge
    if payload.bonus_amount     is not None: g.bonus_amount     = payload.bonus_amount
    if payload.hra_amount       is not None: g.hra_amount       = payload.hra_amount
    if payload.other_allowances is not None: g.other_allowances = payload.other_allowances
    if payload.salary_notes     is not None: g.salary_notes     = payload.salary_notes
    if payload.rules is not None:
        await db.execute(CommissionGroupRule.__table__.delete().where(CommissionGroupRule.group_id == group_id))
        # Salary groups never get service rules
        rules_to_save = [] if g.is_salary_group else payload.rules
        for r in rules_to_save:
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
        "created_at": iso(r.created_at) if r.created_at else None,
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


# ═══════════════════════════════════════════════════════════════════════════════
# SALARY SETTLEMENT ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

class SalarySettlePayload(BaseModel):
    technician_id:   str
    month:           int
    year:            int
    base_salary:     float
    petrol_amount:   float = 0.0
    mobile_recharge: float = 0.0
    bonus_amount:    float = 0.0
    hra_amount:      float = 0.0
    other_allowances:float = 0.0
    deductions:      float = 0.0
    admin_notes:     Optional[str] = None

class SalaryPayoutPayload(BaseModel):
    payout_method:    str   # UPI | BANK
    payout_reference: Optional[str] = None
    payout_notes:     Optional[str] = None
    include_wallet_balance: bool = False  # if True, sweep full wallet balance


@router.get("/salary/technicians", summary="List salary-group technicians with monthly stats [Admin]")
async def list_salary_technicians(
    month: int = Query(default=None),
    year:  int = Query(default=None),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """Returns all technicians assigned to salary groups with their monthly
    attendance summary and booking count for the given month/year."""
    from app.models.commission import CommissionGroup, CommissionGroupAssignment, SalarySettlement
    from app.models.technician import Technician
    from app.models.booking import Booking, BookingStatus
    from app.models.wallet import Wallet
    from datetime import date
    import calendar

    now = date.today()
    m = month or now.month
    y = year  or now.year

    # Fetch all salary groups
    salary_groups = (await db.execute(
        select(CommissionGroup).where(
            CommissionGroup.is_salary_group == True,
            CommissionGroup.is_active == True,
        )
    )).scalars().all()
    group_map = {str(g.id): g for g in salary_groups}

    # Fetch all assignments for salary groups
    assignments = (await db.execute(
        select(CommissionGroupAssignment).where(
            CommissionGroupAssignment.group_id.in_([g.id for g in salary_groups])
        )
    )).scalars().all()
    if not assignments:
        return success_response(data=[], meta={"month": m, "year": y})

    tech_ids = [a.technician_id for a in assignments]
    group_by_tech = {str(a.technician_id): str(a.group_id) for a in assignments}

    # Fetch technicians
    techs = (await db.execute(
        select(Technician).where(Technician.id.in_(tech_ids))
    )).scalars().all()

    # Attendance summary per technician for the month (from attendance_records if exists)
    # Use raw SQL via text for simplicity
    from sqlalchemy import text
    # Try to query attendance table — it may be named differently
    att_stats: dict = {}
    try:
        att_rows = (await db.execute(text("""
            SELECT technician_id,
                   COUNT(*) AS total_days,
                   SUM(CASE WHEN status='PRESENT' THEN 1 ELSE 0 END) AS present_days,
                   SUM(CASE WHEN status='ABSENT'  THEN 1 ELSE 0 END) AS absent_days,
                   SUM(CASE WHEN status='LEAVE'   THEN 1 ELSE 0 END) AS leave_days,
                   COALESCE(SUM(hours_worked), 0) AS total_hours
            FROM attendance_records
            WHERE EXTRACT(MONTH FROM date) = :m
              AND EXTRACT(YEAR  FROM date) = :y
              AND technician_id = ANY(:ids)
            GROUP BY technician_id
        """), {"m": m, "y": y, "ids": [str(t) for t in tech_ids]})).fetchall()
        for row in att_rows:
            att_stats[str(row.technician_id)] = {
                "total_days": row.total_days or 0,
                "present_days": row.present_days or 0,
                "absent_days": row.absent_days or 0,
                "leave_days": row.leave_days or 0,
                "total_hours": round(float(row.total_hours or 0), 1),
            }
    except Exception:
        pass  # attendance table may not exist yet

    # Booking count per technician for the month
    from datetime import datetime, timezone
    start_dt = datetime(y, m, 1, tzinfo=timezone.utc)
    end_dt   = datetime(y, m, calendar.monthrange(y, m)[1], 23, 59, 59, tzinfo=timezone.utc)
    bk_rows = (await db.execute(
        select(Booking.technician_id, func.count(Booking.id).label("cnt"))
        .where(
            Booking.technician_id.in_(tech_ids),
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        )
        .group_by(Booking.technician_id)
    )).all()
    booking_count = {str(r.technician_id): r.cnt for r in bk_rows}

    # Existing salary settlements for this month
    settlements = (await db.execute(
        select(SalarySettlement).where(
            SalarySettlement.technician_id.in_(tech_ids),
            SalarySettlement.month == m,
            SalarySettlement.year  == y,
        )
    )).scalars().all()
    settle_map = {str(s.technician_id): s for s in settlements}

    # Wallet balances
    wallets = (await db.execute(
        select(Wallet).where(Wallet.technician_id.in_(tech_ids))
    )).scalars().all()
    wallet_map = {str(w.technician_id): w for w in wallets}

    result = []
    for tech in techs:
        tid = str(tech.id)
        grp = group_map.get(group_by_tech.get(tid, ""))
        att = att_stats.get(tid, {})
        ss  = settle_map.get(tid)
        wallet = wallet_map.get(tid)
        result.append({
            "technician_id":   tid,
            "name":            tech.name,
            "mobile":          tech.mobile,
            "technician_code": tech.technician_code,
            "profile_image":   tech.profile_image,
            "payout_upi_id":   getattr(tech, "payout_upi_id", None),
            "payout_bank_account": getattr(tech, "payout_bank_account", None),
            "payout_bank_name":    getattr(tech, "payout_bank_name", None),
            "payout_bank_ifsc":    getattr(tech, "payout_bank_ifsc", None),
            "payout_account_holder": getattr(tech, "payout_account_holder", None),
            "group": {
                "id":              str(grp.id),
                "name":            grp.name,
                "monthly_salary":  grp.monthly_salary,
                "petrol_amount":   grp.petrol_amount or 0,
                "mobile_recharge": grp.mobile_recharge or 0,
                "bonus_amount":    grp.bonus_amount or 0,
                "hra_amount":      grp.hra_amount or 0,
                "other_allowances":grp.other_allowances or 0,
                "salary_notes":    grp.salary_notes,
            } if grp else None,
            "attendance":  att,
            "total_bookings": booking_count.get(tid, 0),
            "wallet_balance": round(float(wallet.balance or 0), 2) if wallet else 0.0,
            "settlement": {
                "id":             str(ss.id),
                "status":         ss.status,
                "total_salary":   ss.total_salary,
                "base_salary":    ss.base_salary,
                "petrol_amount":  ss.petrol_amount,
                "mobile_recharge":ss.mobile_recharge,
                "bonus_amount":   ss.bonus_amount,
                "hra_amount":     ss.hra_amount,
                "other_allowances":ss.other_allowances,
                "deductions":     ss.deductions,
                "payout_status":  ss.payout_status,
                "payout_method":  ss.payout_method,
                "payout_reference":ss.payout_reference,
                "payout_amount":  ss.payout_amount,
                "admin_notes":    ss.admin_notes,
                "created_at":     iso(ss.created_at) if ss.created_at else None,
            } if ss else None,
        })

    return success_response(data=result, meta={"month": m, "year": y})


@router.get("/salary/technicians/{technician_id}/report", summary="Salary report for one technician [Admin]")
async def salary_technician_report(
    technician_id: UUID,
    month: int = Query(...),
    year:  int = Query(...),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """Full report: technician profile + attendance details + bookings + salary settlement snapshot."""
    from app.models.commission import CommissionGroup, CommissionGroupAssignment, SalarySettlement
    from app.models.technician import Technician
    from app.models.booking import Booking, BookingStatus
    from app.models.wallet import Wallet, WalletTransaction
    from sqlalchemy import text
    import calendar
    from datetime import datetime, timezone

    tech = (await db.execute(select(Technician).where(Technician.id == technician_id))).scalar_one_or_none()
    if not tech: raise HTTPException(404, "Technician not found")

    # Group
    assign = (await db.execute(
        select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == technician_id)
    )).scalars().first()
    grp = None
    if assign:
        grp = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == assign.group_id))).scalar_one_or_none()

    # Attendance records for month
    att_records = []
    att_summary = {}
    try:
        rows = (await db.execute(text("""
            SELECT date, check_in, check_out, hours_worked, status, notes
            FROM attendance_records
            WHERE technician_id = :tid
              AND EXTRACT(MONTH FROM date) = :m
              AND EXTRACT(YEAR  FROM date) = :y
            ORDER BY date
        """), {"tid": str(technician_id), "m": month, "y": year})).fetchall()
        att_records = [{"date": str(r.date), "check_in": str(r.check_in) if r.check_in else None,
                        "check_out": str(r.check_out) if r.check_out else None,
                        "hours_worked": round(float(r.hours_worked or 0), 2),
                        "status": r.status, "notes": r.notes} for r in rows]
        present = sum(1 for r in att_records if r["status"] == "PRESENT")
        absent  = sum(1 for r in att_records if r["status"] == "ABSENT")
        leave   = sum(1 for r in att_records if r["status"] == "LEAVE")
        att_summary = {
            "total_days": len(att_records),
            "present_days": present,
            "absent_days": absent,
            "leave_days": leave,
            "total_hours": round(sum(r["hours_worked"] for r in att_records), 1),
        }
    except Exception:
        pass

    # Bookings for month
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    end_dt   = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=timezone.utc)
    bookings = (await db.execute(
        select(Booking).where(
            Booking.technician_id == technician_id,
            Booking.created_at >= start_dt,
            Booking.created_at <= end_dt,
        ).order_by(Booking.created_at.desc())
    )).scalars().all()

    # Wallet
    wallet = (await db.execute(select(Wallet).where(Wallet.technician_id == technician_id))).scalar_one_or_none()
    wallet_txns = []
    if wallet:
        txn_rows = (await db.execute(
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet.id)
            .order_by(WalletTransaction.created_at.desc())
            .limit(20)
        )).scalars().all()
        wallet_txns = [{"id": str(t.id), "type": t.transaction_type, "amount": t.amount,
                        "description": t.description, "created_at": iso(t.created_at)} for t in txn_rows]

    # Existing settlement
    ss = (await db.execute(
        select(SalarySettlement).where(
            SalarySettlement.technician_id == technician_id,
            SalarySettlement.month == month,
            SalarySettlement.year  == year,
        )
    )).scalar_one_or_none()

    return success_response(data={
        "technician": {
            "id": str(tech.id), "name": tech.name, "mobile": tech.mobile,
            "email": getattr(tech, "email", None),
            "profile_image": tech.profile_image,
            "payout_upi_id": getattr(tech, "payout_upi_id", None),
            "payout_bank_account": getattr(tech, "payout_bank_account", None),
            "payout_bank_ifsc":    getattr(tech, "payout_bank_ifsc", None),
            "payout_bank_name":    getattr(tech, "payout_bank_name", None),
            "payout_account_holder": getattr(tech, "payout_account_holder", None),
        },
        "group": {
            "id": str(grp.id), "name": grp.name,
            "monthly_salary":  grp.monthly_salary,
            "petrol_amount":   grp.petrol_amount or 0,
            "mobile_recharge": grp.mobile_recharge or 0,
            "bonus_amount":    grp.bonus_amount or 0,
            "hra_amount":      grp.hra_amount or 0,
            "other_allowances":grp.other_allowances or 0,
            "salary_notes":    grp.salary_notes,
        } if grp else None,
        "period": {"month": month, "year": year},
        "attendance_records": att_records,
        "attendance_summary": att_summary,
        "bookings": [{
            "id": str(b.id), "booking_number": b.booking_number,
            "status": b.status.value if b.status else b.status,
            "created_at": iso(b.created_at),
        } for b in bookings],
        "wallet": {
            "balance": round(float(wallet.balance or 0), 2),
            "total_earned": round(float(wallet.total_earned or 0), 2),
            "total_withdrawn": round(float(wallet.total_withdrawn or 0), 2),
        } if wallet else None,
        "wallet_transactions": wallet_txns,
        "settlement": {
            "id": str(ss.id), "status": ss.status,
            "base_salary": ss.base_salary, "petrol_amount": ss.petrol_amount,
            "mobile_recharge": ss.mobile_recharge, "bonus_amount": ss.bonus_amount,
            "hra_amount": ss.hra_amount, "other_allowances": ss.other_allowances,
            "deductions": ss.deductions, "total_salary": ss.total_salary,
            "payout_status": ss.payout_status, "payout_method": ss.payout_method,
            "payout_reference": ss.payout_reference, "payout_amount": ss.payout_amount,
            "admin_notes": ss.admin_notes,
            "created_at": iso(ss.created_at) if ss.created_at else None,
        } if ss else None,
    })


@router.post("/salary/settle", summary="Generate salary settlement and credit wallet [Admin]")
async def create_salary_settlement(
    payload: SalarySettlePayload,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import CommissionGroup, CommissionGroupAssignment, SalarySettlement
    from app.models.technician import Technician
    from app.models.wallet import Wallet, WalletTransaction

    tech_id = UUID(payload.technician_id)
    tech = (await db.execute(select(Technician).where(Technician.id == tech_id))).scalar_one_or_none()
    if not tech: raise HTTPException(404, "Technician not found")

    # Validate salary group membership
    assign = (await db.execute(
        select(CommissionGroupAssignment).where(CommissionGroupAssignment.technician_id == tech_id)
    )).scalars().first()
    if not assign:
        raise HTTPException(400, "Technician is not assigned to any commission group")
    grp = (await db.execute(select(CommissionGroup).where(CommissionGroup.id == assign.group_id))).scalar_one_or_none()
    if not grp or not grp.is_salary_group:
        raise HTTPException(400, "Technician is not in a salary group")

    # Prevent double settlement
    existing = (await db.execute(
        select(SalarySettlement).where(
            SalarySettlement.technician_id == tech_id,
            SalarySettlement.month == payload.month,
            SalarySettlement.year  == payload.year,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"Salary already settled for {payload.month}/{payload.year}")

    total = (
        payload.base_salary + payload.petrol_amount + payload.mobile_recharge +
        payload.bonus_amount + payload.hra_amount + payload.other_allowances -
        payload.deductions
    )
    total = max(0.0, total)

    ss = SalarySettlement(
        technician_id=tech_id, group_id=assign.group_id,
        month=payload.month, year=payload.year,
        base_salary=payload.base_salary, petrol_amount=payload.petrol_amount,
        mobile_recharge=payload.mobile_recharge, bonus_amount=payload.bonus_amount,
        hra_amount=payload.hra_amount, other_allowances=payload.other_allowances,
        deductions=payload.deductions, total_salary=total,
        status="PAID", admin_notes=payload.admin_notes,
        settled_by=UUID(current_user["user_id"]),
    )
    db.add(ss)
    await db.flush()

    # Credit wallet
    w = (await db.execute(select(Wallet).where(Wallet.technician_id == tech_id))).scalar_one_or_none()
    if not w:
        w = Wallet(technician_id=tech_id, user_id=tech.user_id, balance=0.0, total_earned=0.0, total_withdrawn=0.0)
        db.add(w); await db.flush()
    before = w.balance or 0
    w.balance = round(before + total, 2)
    w.total_earned = round((w.total_earned or 0) + total, 2)
    txn = WalletTransaction(
        wallet_id=w.id, transaction_type="SALARY",
        amount=total, balance_before=before, balance_after=w.balance,
        reference_id=str(ss.id),
        description=f"Monthly salary for {payload.month}/{payload.year} — {grp.name}. {payload.admin_notes or ''}".strip(),
        status="SUCCESS",
    )
    db.add(txn); await db.flush()
    ss.wallet_txn_id = txn.id
    await db.commit()

    # FCM push
    try:
        from app.utils.fcm import send_simple_push
        if getattr(tech, "fcm_token", None):
            await send_simple_push(
                fcm_token=tech.fcm_token,
                title="Salary Credited 💼",
                body=f"Your salary of ₹{total:.0f} for {payload.month}/{payload.year} has been credited to your wallet.",
                data={"type": "SALARY_CREDITED", "amount": str(total)},
            )
    except Exception:
        pass

    return success_response(data={"id": str(ss.id), "total_salary": total, "wallet_balance": w.balance}, message="Salary settled and credited to wallet")


@router.post("/salary/settlements/{settlement_id}/payout", summary="Send salary from wallet to bank/UPI [Admin]")
async def salary_payout(
    settlement_id: UUID,
    payload: SalaryPayoutPayload,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    """Debit the technician's wallet (salary + any other wallet balance if requested)
    and mark the settlement as paid out to bank/UPI."""
    from app.models.commission import SalarySettlement
    from app.models.technician import Technician
    from app.models.wallet import Wallet, WalletTransaction
    from datetime import datetime, timezone

    ss = (await db.execute(select(SalarySettlement).where(SalarySettlement.id == settlement_id))).scalar_one_or_none()
    if not ss: raise HTTPException(404, "Settlement not found")
    if ss.status != "PAID": raise HTTPException(400, "Salary has not been credited to wallet yet")
    if ss.payout_status == "PAID": raise HTTPException(400, "Already paid out")

    tech = (await db.execute(select(Technician).where(Technician.id == ss.technician_id))).scalar_one_or_none()
    if not tech: raise HTTPException(404, "Technician not found")

    w = (await db.execute(select(Wallet).where(Wallet.technician_id == ss.technician_id))).scalar_one_or_none()
    if not w: raise HTTPException(404, "Wallet not found")

    # Amount to send: full wallet balance (includes salary + market reimbursements etc.)
    # or just the salary amount
    send_amount = round(float(w.balance or 0), 2) if payload.include_wallet_balance else round(float(ss.total_salary), 2)
    if send_amount <= 0:
        raise HTTPException(400, "Nothing to pay out — wallet balance is zero")
    if (w.balance or 0) < send_amount:
        raise HTTPException(400, f"Insufficient wallet balance ₹{w.balance:.2f} for payout of ₹{send_amount:.2f}")

    if payload.payout_method not in ("UPI", "BANK"):
        raise HTTPException(400, "payout_method must be UPI or BANK")

    before = w.balance or 0
    w.balance = round(before - send_amount, 2)
    w.total_withdrawn = round((w.total_withdrawn or 0) + send_amount, 2)

    txn = WalletTransaction(
        wallet_id=w.id, transaction_type="WITHDRAWAL",
        amount=send_amount, balance_before=before, balance_after=w.balance,
        reference_id=payload.payout_reference,
        description=f"Salary payout via {payload.payout_method} for {ss.month}/{ss.year}. Ref: {payload.payout_reference or 'N/A'}. {payload.payout_notes or ''}".strip(),
        status="SUCCESS",
    )
    db.add(txn)

    ss.payout_status    = "PAID"
    ss.payout_method    = payload.payout_method
    ss.payout_reference = payload.payout_reference
    ss.payout_amount    = send_amount
    ss.payout_notes     = payload.payout_notes
    ss.payout_at        = datetime.now(timezone.utc)

    await db.commit()

    # FCM
    try:
        from app.utils.fcm import send_simple_push
        upi_or_bank = getattr(tech, "payout_upi_id", None) if payload.payout_method == "UPI" else getattr(tech, "payout_bank_account", None)
        if getattr(tech, "fcm_token", None):
            await send_simple_push(
                fcm_token=tech.fcm_token,
                title="Salary Transferred 🏦",
                body=f"₹{send_amount:.0f} has been sent to your {payload.payout_method}{(' — ' + str(upi_or_bank)) if upi_or_bank else ''}.",
                data={"type": "SALARY_PAYOUT", "amount": str(send_amount)},
            )
    except Exception:
        pass

    return success_response(data={"payout_amount": send_amount, "wallet_balance": w.balance}, message="Salary paid out successfully")


@router.get("/salary/settlements", summary="List all salary settlements [Admin]")
async def list_salary_settlements(
    month: Optional[int] = Query(None),
    year:  Optional[int] = Query(None),
    technician_id: Optional[str] = Query(None),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.commission import SalarySettlement
    q = select(SalarySettlement)
    if month: q = q.where(SalarySettlement.month == month)
    if year:  q = q.where(SalarySettlement.year  == year)
    if technician_id: q = q.where(SalarySettlement.technician_id == UUID(technician_id))
    rows = (await db.execute(q.order_by(SalarySettlement.created_at.desc()))).scalars().all()
    return success_response(data=[{
        "id": str(s.id), "technician_id": str(s.technician_id),
        "month": s.month, "year": s.year, "status": s.status,
        "total_salary": s.total_salary, "payout_status": s.payout_status,
        "payout_method": s.payout_method, "payout_reference": s.payout_reference,
        "payout_amount": s.payout_amount, "created_at": iso(s.created_at) if s.created_at else None,
    } for s in rows])
