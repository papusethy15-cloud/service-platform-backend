from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()


class CreditWalletRequest(BaseModel):
    user_id:      Optional[str] = None
    technician_id: Optional[str] = None
    amount:       float
    description:  Optional[str] = None
    reference_id: Optional[str] = None


class DebitWalletRequest(BaseModel):
    wallet_id:   str
    amount:      float
    description: Optional[str] = None
    reference_id: Optional[str] = None


class WithdrawRequest(BaseModel):
    amount:       float
    bank_account: Optional[str] = None
    notes:        Optional[str] = None


# ─── helpers ─────────────────────────────────────────────────────
async def _get_or_create_wallet(db, *, user_id=None, technician_id=None):
    from app.models.wallet import Wallet
    if technician_id:
        w = (await db.execute(select(Wallet).where(Wallet.technician_id == technician_id))).scalar_one_or_none()
        if not w:
            w = Wallet(technician_id=technician_id, user_id=user_id, balance=0.0,
                       total_earned=0.0, total_withdrawn=0.0)
            db.add(w); await db.flush()
        return w
    w = (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalars().first()
    if not w:
        w = Wallet(user_id=user_id, balance=0.0, total_earned=0.0, total_withdrawn=0.0)
        db.add(w); await db.flush()
    return w


async def _enrich_wallets(db, wallets):
    """Join technician & user names onto wallet rows."""
    from app.models.technician import Technician
    from app.models.user import User

    tech_ids = [w.technician_id for w in wallets if w.technician_id]
    user_ids  = [w.user_id       for w in wallets if w.user_id]

    tech_map = {}
    if tech_ids:
        rows = (await db.execute(
            select(Technician).where(Technician.id.in_(tech_ids))
        )).scalars().all()
        tech_map = {str(t.id): t for t in rows}

    user_map = {}
    if user_ids:
        rows = (await db.execute(
            select(User).where(User.id.in_(user_ids))
        )).scalars().all()
        user_map = {str(u.id): u for u in rows}

    result = []
    for w in wallets:
        tech = tech_map.get(str(w.technician_id)) if w.technician_id else None
        user = user_map.get(str(w.user_id))       if w.user_id       else None
        result.append({
            "id":              str(w.id),
            "technician_id":   str(w.technician_id) if w.technician_id else None,
            "technician_name": tech.name if tech else None,
            "technician_code": tech.technician_code if tech else None,
            "technician_mobile": tech.mobile if tech else None,
            "user_id":         str(w.user_id) if w.user_id else None,
            "user_name":       user.name if user else (user.email if user else None),
            "balance":         round(w.balance or 0, 2),
            "total_earned":    round(w.total_earned or 0, 2),
            "total_withdrawn": round(w.total_withdrawn or 0, 2),
            "is_active":       w.is_active,
            "updated_at":      w.updated_at.isoformat() if w.updated_at else None,
            "created_at":      w.created_at.isoformat() if w.created_at else None,
        })
    return result


# ─── Public: my wallet ────────────────────────────────────────────
@router.get("/me", summary="My wallet balance")
async def my_wallet(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    w = await _get_or_create_wallet(db, user_id=UUID(current_user["user_id"]))
    return success_response(data={
        "balance": w.balance,
        "total_earned": w.total_earned,
        "total_withdrawn": w.total_withdrawn,
    })


@router.get("/me/transactions", summary="My wallet transactions")
async def my_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import WalletTransaction
    w = await _get_or_create_wallet(db, user_id=UUID(current_user["user_id"]))
    q = (select(WalletTransaction)
         .where(WalletTransaction.wallet_id == w.id)
         .order_by(WalletTransaction.created_at.desc()))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    txns  = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return success_response(data={
        "items": [{
            "id":            str(t.id),
            "type":          t.transaction_type,
            "amount":        t.amount,
            "balance_after": t.balance_after,
            "description":   t.description,
            "status":        t.status,
            "created_at":    t.created_at.isoformat(),
        } for t in txns],
        "total": total,
    })


# ─── Admin: list all wallets ──────────────────────────────────────
@router.get("", summary="All wallets [Admin]")
async def list_wallets(
    page:     int  = Query(1, ge=1),
    per_page: int  = Query(20, ge=1, le=100),
    search:   str  = Query(None),
    sort_by:  str  = Query("balance", description="balance | total_earned | total_withdrawn | updated_at"),
    sort_dir: str  = Query("desc"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet
    from app.models.technician import Technician

    q = select(Wallet).where(Wallet.is_active == True)

    # Sort
    sort_col = {
        "balance":         Wallet.balance,
        "total_earned":    Wallet.total_earned,
        "total_withdrawn": Wallet.total_withdrawn,
        "updated_at":      Wallet.updated_at,
    }.get(sort_by, Wallet.balance)
    q = q.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())

    total = (await db.execute(select(func.count()).select_from(
        select(Wallet).where(Wallet.is_active == True).subquery()
    ))).scalar_one()
    wallets = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()

    items = await _enrich_wallets(db, wallets)

    # Client-side search filter on name/code (after enrichment)
    if search:
        s = search.lower()
        items = [i for i in items if s in (i["technician_name"] or "").lower()
                 or s in (i["technician_code"] or "").lower()
                 or s in (i["user_name"] or "").lower()]

    # Aggregate summary
    agg = (await db.execute(
        select(
            func.coalesce(func.sum(Wallet.balance), 0).label("total_balance"),
            func.coalesce(func.sum(Wallet.total_earned), 0).label("total_earned"),
            func.coalesce(func.sum(Wallet.total_withdrawn), 0).label("total_withdrawn"),
            func.count(Wallet.id).label("wallet_count"),
        ).where(Wallet.is_active == True)
    )).one()

    return success_response(data={
        "items":    items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "summary": {
            "total_balance":    round(float(agg.total_balance), 2),
            "total_earned":     round(float(agg.total_earned), 2),
            "total_withdrawn":  round(float(agg.total_withdrawn), 2),
            "wallet_count":     agg.wallet_count,
        },
    })


# ─── Admin: single wallet detail + transactions ───────────────────
@router.get("/{wallet_id}/transactions", summary="Transactions for a wallet [Admin]")
async def wallet_transactions(
    wallet_id: UUID,
    page:      int = Query(1, ge=1),
    per_page:  int = Query(30, ge=1, le=200),
    txn_type:  str = Query(None, description="CREDIT | DEBIT | WITHDRAWAL | REFUND"),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet, WalletTransaction

    wallet = (await db.execute(select(Wallet).where(Wallet.id == wallet_id))).scalar_one_or_none()
    if not wallet:
        raise HTTPException(404, "Wallet not found")

    q = select(WalletTransaction).where(WalletTransaction.wallet_id == wallet_id)
    if txn_type:
        q = q.where(WalletTransaction.transaction_type == txn_type)
    q = q.order_by(WalletTransaction.created_at.desc())

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    txns  = (await db.execute(q.offset((page - 1) * per_page).limit(per_page))).scalars().all()

    enriched = await _enrich_wallets(db, [wallet])
    wallet_info = enriched[0] if enriched else {}

    return success_response(data={
        "wallet":  wallet_info,
        "total":   total,
        "page":    page,
        "per_page": per_page,
        "pages":   max(1, (total + per_page - 1) // per_page),
        "items": [{
            "id":            str(t.id),
            "type":          t.transaction_type,
            "amount":        t.amount,
            "balance_after": t.balance_after,
            "description":   t.description,
            "reference_id":  t.reference_id,
            "status":        t.status,
            "created_at":    t.created_at.isoformat(),
        } for t in txns],
    })


# ─── Admin: credit ────────────────────────────────────────────────
@router.post("/credit", summary="Credit wallet [Admin]")
async def credit_wallet(
    payload: CreditWalletRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import WalletTransaction

    if not payload.user_id and not payload.technician_id:
        raise HTTPException(400, "Provide user_id or technician_id")

    tech_uuid = UUID(payload.technician_id) if payload.technician_id else None
    user_uuid = UUID(payload.user_id)       if payload.user_id       else None

    w = await _get_or_create_wallet(db, user_id=user_uuid, technician_id=tech_uuid)
    balance_before = w.balance or 0
    w.balance      = round(balance_before + payload.amount, 2)
    w.total_earned = round((w.total_earned or 0) + payload.amount, 2)

    db.add(WalletTransaction(
        wallet_id=w.id,
        transaction_type="CREDIT",
        amount=payload.amount,
        balance_before=balance_before,
        balance_after=w.balance,
        description=payload.description or "Manual credit by admin",
        reference_id=payload.reference_id,
    ))
    await db.commit()
    return success_response(data={"balance": w.balance}, message="Wallet credited")


# ─── Admin: debit ─────────────────────────────────────────────────
@router.post("/debit", summary="Debit wallet [Admin]")
async def debit_wallet(
    payload: DebitWalletRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import Wallet, WalletTransaction

    w = (await db.execute(select(Wallet).where(Wallet.id == UUID(payload.wallet_id)))).scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Wallet not found")
    if (w.balance or 0) < payload.amount:
        raise HTTPException(400, f"Insufficient balance: ₹{w.balance:.2f} available")

    balance_before  = w.balance or 0
    w.balance       = round(balance_before - payload.amount, 2)
    w.total_withdrawn = round((w.total_withdrawn or 0) + payload.amount, 2)

    db.add(WalletTransaction(
        wallet_id=w.id,
        transaction_type="DEBIT",
        amount=payload.amount,
        balance_before=balance_before,
        balance_after=w.balance,
        description=payload.description or "Manual debit by admin",
        reference_id=payload.reference_id,
    ))
    await db.commit()
    return success_response(data={"balance": w.balance}, message="Wallet debited")


# ─── Public: withdraw ─────────────────────────────────────────────
@router.post("/withdraw", summary="Request withdrawal")
async def request_withdrawal(
    payload: WithdrawRequest,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.wallet import WalletTransaction

    w = await _get_or_create_wallet(db, user_id=UUID(current_user["user_id"]))
    if (w.balance or 0) < payload.amount:
        raise HTTPException(400, "Insufficient balance")

    balance_before  = w.balance or 0
    w.balance       = round(balance_before - payload.amount, 2)
    w.total_withdrawn = round((w.total_withdrawn or 0) + payload.amount, 2)

    db.add(WalletTransaction(
        wallet_id=w.id,
        transaction_type="WITHDRAWAL",
        amount=payload.amount,
        balance_before=balance_before,
        balance_after=w.balance,
        description=payload.notes,
        status="PENDING",
    ))
    await db.commit()
    return success_response(
        data={"balance": w.balance},
        message="Withdrawal requested",
    )
