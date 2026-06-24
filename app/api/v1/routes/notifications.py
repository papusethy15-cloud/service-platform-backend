from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response

router = APIRouter()

class SendNotificationRequest(BaseModel):
    user_id: str; title: str; body: str; channel: str = "PUSH"
    data: Optional[dict] = None

class BulkNotificationRequest(BaseModel):
    role: Optional[str] = None; user_ids: Optional[List[str]] = None
    title: str; body: str; channel: str = "PUSH"

class CreateTemplateRequest(BaseModel):
    name: str; title: str; body: str; channel: str = "PUSH"

@router.get("", summary="My notifications")
async def list_notifications(page: int = Query(1, ge=1), per_page: int = Query(20), is_read: bool = Query(None), current_user: dict = Depends(AnyAuthenticated), db: AsyncSession = Depends(get_db)):
    from app.models.notification import Notification
    q = select(Notification).where(Notification.user_id == UUID(current_user["user_id"]))
    if is_read is not None: q = q.where(Notification.is_read == is_read)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    notifs = (await db.execute(q.order_by(Notification.created_at.desc()).offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(n.id), "title": n.title, "body": n.body, "is_read": n.is_read, "created_at": n.created_at.isoformat()} for n in notifs], "total": total})

@router.post("/send", summary="Send notification [Admin/CCO]")
async def send_notification(payload: SendNotificationRequest, current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.notification import Notification
    notif = Notification(user_id=UUID(payload.user_id), title=payload.title, body=payload.body, channel=payload.channel)
    db.add(notif); await db.commit()
    return success_response(data={"id": str(notif.id)}, message="Notification sent")

@router.post("/bulk", summary="Send bulk notification [Admin]")
async def bulk_notification(payload: BulkNotificationRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.notification import Notification
    from app.models.user import User
    q = select(User).where(User.is_active == True)
    if payload.role: q = q.where(User.role == payload.role)
    elif payload.user_ids: q = q.where(User.id.in_([UUID(uid) for uid in payload.user_ids]))
    users = (await db.execute(q)).scalars().all()
    for user in users:
        db.add(Notification(user_id=user.id, title=payload.title, body=payload.body, channel=payload.channel))
    await db.commit()
    return success_response(data={"sent_to": len(users)}, message=f"Notification sent to {len(users)} users")

@router.get("/templates", summary="Notification templates [Admin]")
async def list_templates(current_user: dict = Depends(AdminOrCCO), db: AsyncSession = Depends(get_db)):
    from app.models.notification import NotificationTemplate
    templates = (await db.execute(select(NotificationTemplate).where(NotificationTemplate.is_active == True))).scalars().all()
    return success_response(data=[{"id": str(t.id), "name": t.name, "title": t.title, "body": t.body, "channel": t.channel} for t in templates])

@router.post("/templates", summary="Create notification template [Admin]")
async def create_template(payload: CreateTemplateRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.notification import NotificationTemplate
    tmpl = NotificationTemplate(name=payload.name, title=payload.title, body=payload.body, channel=payload.channel)
    db.add(tmpl); await db.commit()
    return success_response(data={"id": str(tmpl.id)}, message="Template created")
