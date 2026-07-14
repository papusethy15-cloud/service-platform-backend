from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from uuid import UUID
from pydantic import BaseModel
from typing import Optional, List
from app.core.database import get_db
from app.api.deps import AdminOnly, AdminOrCCO, AnyAuthenticated
from app.utils.response import success_response, iso

router = APIRouter()


def _serialize(n) -> dict:
    """Serialize a Notification row to the API shape the app expects."""
    notif_type = "SYSTEM"
    if n.data and isinstance(n.data, dict):
        t = n.data.get("type", "")
        if "ASSIGNMENT" in t or "JOB" in t:
            notif_type = "ASSIGNMENT"
        elif "PAYMENT" in t or "WALLET" in t:
            notif_type = "PAYMENT"
        elif "BOOKING" in t:
            notif_type = "BOOKING"
        elif "LEAVE" in t:
            notif_type = "LEAVE"
    return {
        "id":                str(n.id),
        "title":             n.title,
        "body":              n.body,
        "is_read":           n.is_read,
        "notification_type": notif_type,
        "channel":           n.channel,
        "data":              n.data,
        "created_at":        iso(n.created_at) if n.created_at else None,
    }


# ── GET /notifications ────────────────────────────────────────────────────────
@router.get("", summary="My notifications")
async def list_notifications(
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    is_read: Optional[bool] = Query(None),
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import Notification
    q = select(Notification).where(Notification.user_id == UUID(current_user["user_id"]))
    if is_read is not None:
        q = q.where(Notification.is_read == is_read)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    unread = (await db.execute(
        select(func.count()).select_from(
            select(Notification).where(
                Notification.user_id == UUID(current_user["user_id"]),
                Notification.is_read == False,
            ).subquery()
        )
    )).scalar_one()
    notifs = (await db.execute(
        q.order_by(Notification.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()
    return success_response(data={
        "items":       [_serialize(n) for n in notifs],
        "total":       total,
        "unread":      unread,
        "page":        page,
        "per_page":    per_page,
        "has_more":    len(notifs) >= per_page,
    })


# ── POST /notifications/{id}/read ────────────────────────────────────────────
@router.post("/{notification_id}/read", summary="Mark notification as read")
async def mark_read(
    notification_id: str,
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import Notification
    notif = (await db.execute(
        select(Notification).where(
            Notification.id == UUID(notification_id),
            Notification.user_id == UUID(current_user["user_id"]),
        )
    )).scalar_one_or_none()
    if notif:
        notif.is_read = True
        await db.commit()
    return success_response(message="Marked as read")


# ── POST /notifications/read-all ─────────────────────────────────────────────
@router.post("/read-all", summary="Mark all notifications as read")
async def mark_all_read(
    current_user: dict = Depends(AnyAuthenticated),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import Notification
    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == UUID(current_user["user_id"]),
            Notification.is_read == False,
        )
        .values(is_read=True)
    )
    await db.commit()
    return success_response(message="All notifications marked as read")


# ── GET /notifications/admin-log [Admin/CCO] ────────────────────────────────
@router.get("/admin-log", summary="All notifications log [Admin/CCO]")
async def admin_notification_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(25),
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    """Returns ALL notifications in the system for admin oversight, newest first."""
    from app.models.notification import Notification
    from app.models.user import User
    base_q = select(Notification).order_by(Notification.created_at.desc())
    total = (await db.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()
    notifs = (await db.execute(
        base_q.offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    user_ids = list({n.user_id for n in notifs})
    users = {}
    if user_ids:
        rows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        users = {u.id: u for u in rows}
    items = []
    for n in notifs:
        u = users.get(n.user_id)
        items.append({
            **_serialize(n),
            "recipient_name": u.name if u else None,
            "recipient_role": u.role.value if u and u.role else None,
            "recipient_mobile": u.mobile if u else None,
            "sent_at": iso(n.created_at) if n.created_at else None,
            "status": "SENT",
        })
    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "has_more": (page * per_page) < total,
    })


# ── POST /notifications/send [Admin/CCO] ─────────────────────────────────────
class SendNotificationRequest(BaseModel):
    user_id: str
    title: str
    body: str
    channel: str = "PUSH"
    data: Optional[dict] = None

@router.post("/send", summary="Send notification to a specific user [Admin/CCO]")
async def send_notification(
    payload: SendNotificationRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import Notification
    from app.models.user import User

    # Save in-app notification record
    notif = Notification(
        user_id=UUID(payload.user_id),
        title=payload.title,
        body=payload.body,
        channel=payload.channel,
        data=payload.data or {"type": "ADMIN_BROADCAST"},
    )
    db.add(notif)
    await db.commit()

    # Send FCM push if channel is PUSH
    push_sent = False
    if payload.channel == "PUSH":
        user = (await db.execute(
            select(User).where(User.id == UUID(payload.user_id))
        )).scalar_one_or_none()
        if user and user.fcm_token:
            from app.utils.fcm import send_simple_push
            push_sent = await send_simple_push(
                fcm_token=user.fcm_token,
                title=payload.title,
                body=payload.body,
                data=payload.data or {"type": "ADMIN_BROADCAST"},
            )

    return success_response(
        data={"id": str(notif.id), "push_sent": push_sent},
        message="Notification sent" + (" with push" if push_sent else " (in-app only)"),
    )


# ── POST /notifications/bulk [Admin] ─────────────────────────────────────────
class BulkNotificationRequest(BaseModel):
    role: Optional[str] = None
    user_ids: Optional[List[str]] = None
    title: str
    body: str
    channel: str = "PUSH"
    data: Optional[dict] = None

@router.post("/bulk", summary="Send bulk notification [Admin]")
async def bulk_notification(
    payload: BulkNotificationRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import Notification
    from app.models.user import User

    q = select(User).where(User.is_active == True)
    if payload.role:
        q = q.where(User.role == payload.role)
    elif payload.user_ids:
        q = q.where(User.id.in_([UUID(uid) for uid in payload.user_ids]))
    users = (await db.execute(q)).scalars().all()

    notif_data = payload.data or {"type": "ADMIN_BROADCAST"}
    push_sent = 0
    push_failed = 0

    for user in users:
        # Save in-app notification record for every user
        db.add(Notification(
            user_id=user.id,
            title=payload.title,
            body=payload.body,
            channel=payload.channel,
            data=notif_data,
        ))

    await db.commit()

    # Send FCM push to all users with tokens (after DB commit so records are safe)
    if payload.channel == "PUSH":
        from app.utils.fcm import send_simple_push
        import asyncio
        async def _push_one(user):
            if user.fcm_token:
                ok = await send_simple_push(
                    fcm_token=user.fcm_token,
                    title=payload.title,
                    body=payload.body,
                    data=notif_data,
                )
                return ok
            return False

        results = await asyncio.gather(*[_push_one(u) for u in users], return_exceptions=True)
        for r in results:
            if r is True:
                push_sent += 1
            else:
                push_failed += 1

    return success_response(
        data={
            "sent_to":    len(users),
            "push_sent":  push_sent,
            "push_failed": push_failed,
        },
        message=f"Notification sent to {len(users)} users ({push_sent} push delivered)",
    )


# ── GET /notifications/templates [Admin/CCO] ─────────────────────────────────
@router.get("/templates", summary="Notification templates [Admin]")
async def list_templates(
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import NotificationTemplate
    templates = (await db.execute(
        select(NotificationTemplate).where(NotificationTemplate.is_active == True)
    )).scalars().all()
    return success_response(data=[{
        "id": str(t.id), "name": t.name, "title": t.title,
        "body": t.body, "channel": t.channel,
    } for t in templates])


# ── POST /notifications/templates [Admin] ────────────────────────────────────
class CreateTemplateRequest(BaseModel):
    name: str
    title: str
    body: str
    channel: str = "PUSH"

@router.post("/templates", summary="Create notification template [Admin]")
async def create_template(
    payload: CreateTemplateRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notification import NotificationTemplate
    tmpl = NotificationTemplate(
        name=payload.name, title=payload.title,
        body=payload.body, channel=payload.channel,
    )
    db.add(tmpl)
    await db.commit()
    return success_response(data={"id": str(tmpl.id)}, message="Template created")
