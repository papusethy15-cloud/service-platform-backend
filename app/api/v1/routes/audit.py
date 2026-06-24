from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly
from app.utils.response import success_response

router = APIRouter()

@router.get("", summary="Audit logs [Admin]")
async def list_audit_logs(page: int = Query(1, ge=1), per_page: int = Query(50),
                          user_id: Optional[str] = None, action: Optional[str] = None,
                          resource_type: Optional[str] = None,
                          current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.audit import AuditLog
    q = select(AuditLog).order_by(AuditLog.created_at.desc())
    if user_id: q = q.where(AuditLog.user_id == UUID(user_id))
    if action: q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if resource_type: q = q.where(AuditLog.resource_type == resource_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    logs = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(l.id), "user_id": str(l.user_id) if l.user_id else None,
                                              "action": l.action, "resource_type": l.resource_type,
                                              "resource_id": l.resource_id, "ip_address": l.ip_address,
                                              "created_at": l.created_at.isoformat()} for l in logs], "total": total})
