from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminOnly, require_roles
from app.api.v1.schemas.users import (
    CreateInternalUserRequest,
    UpdateInternalUserRequest,
    UpdateRolePermissionsRequest,
    UpdateUserPermissionsRequest,
)
from app.core.database import get_db
from app.core.security import hash_password
from app.models.rbac import Permission, Role, RolePermission, UserPermission
from app.models.user import User, UserRole
from app.utils.response import success_response

router = APIRouter()
SuperAdminOnly = require_roles("SUPER_ADMIN")

MANAGED_INTERNAL_ROLES = {
    UserRole.SUPER_ADMIN.value,
    UserRole.ADMIN.value,
    UserRole.CCO.value,
    UserRole.ACCOUNTANT.value,
    UserRole.INVENTORY_MANAGER.value,
}

ROLE_METADATA = {
    UserRole.SUPER_ADMIN.value: {"name": "Super Admin", "description": "Full platform control"},
    UserRole.ADMIN.value: {"name": "Admin", "description": "Operational administration"},
    UserRole.CCO.value: {"name": "CCO", "description": "Call center operations"},
    UserRole.TECHNICIAN.value: {"name": "Technician", "description": "Field technician user"},
    UserRole.CUSTOMER.value: {"name": "Customer", "description": "End customer user"},
    UserRole.ACCOUNTANT.value: {"name": "Accountant", "description": "Finance and payment operations"},
    UserRole.INVENTORY_MANAGER.value: {"name": "Inventory Manager", "description": "Inventory and warehouse operations"},
}

PERMISSION_CATALOG = [
    {"code": "user.view", "module": "users", "name": "View Users", "description": "View user directory and profiles"},
    {"code": "user.create", "module": "users", "name": "Create Users", "description": "Create internal staff accounts"},
    {"code": "user.update", "module": "users", "name": "Update Users", "description": "Update user information"},
    {"code": "user.deactivate", "module": "users", "name": "Deactivate Users", "description": "Disable internal accounts"},
    {"code": "role.manage", "module": "users", "name": "Manage Roles", "description": "Manage role permissions"},
    {"code": "permission.manage", "module": "users", "name": "Manage Permissions", "description": "Override permissions on individual users"},
    {"code": "customer.view", "module": "customers", "name": "View Customers", "description": "View customers and history"},
    {"code": "customer.manage", "module": "customers", "name": "Manage Customers", "description": "Create and update customers"},
    {"code": "technician.view", "module": "technicians", "name": "View Technicians", "description": "View technician roster"},
    {"code": "technician.manage", "module": "technicians", "name": "Manage Technicians", "description": "Create and update technicians"},
    {"code": "service.manage", "module": "services", "name": "Manage Services", "description": "Manage service catalog"},
    {"code": "booking.view", "module": "bookings", "name": "View Bookings", "description": "View booking queue and history"},
    {"code": "booking.manage", "module": "bookings", "name": "Manage Bookings", "description": "Update booking lifecycle and schedules"},
    {"code": "assignment.manage", "module": "assignments", "name": "Manage Assignments", "description": "Auto and manual technician assignment"},
    {"code": "quotation.manage", "module": "quotations", "name": "Manage Quotations", "description": "Create and approve quotations"},
    {"code": "invoice.manage", "module": "invoices", "name": "Manage Invoices", "description": "Generate and send invoices"},
    {"code": "payment.manage", "module": "payments", "name": "Manage Payments", "description": "Create and verify payments"},
    {"code": "tracking.view", "module": "tracking", "name": "View Tracking", "description": "View technician locations and tracking history"},
    {"code": "tracking.update", "module": "tracking", "name": "Update Tracking", "description": "Push technician GPS coordinates"},
    {"code": "gst.view", "module": "gst", "name": "View GST Settings", "description": "View GST configuration"},
    {"code": "gst.manage", "module": "gst", "name": "Manage GST Settings", "description": "Update GST settings and validation rules"},
    {"code": "report.view", "module": "reports", "name": "View Reports", "description": "View operational and financial reports"},
    {"code": "report.export", "module": "reports", "name": "Export Reports", "description": "Export report data"},
]

DEFAULT_ROLE_PERMISSIONS = {
    UserRole.SUPER_ADMIN.value: [item["code"] for item in PERMISSION_CATALOG],
    UserRole.ADMIN.value: [item["code"] for item in PERMISSION_CATALOG if item["code"] != "tracking.update"],
    UserRole.CCO.value: [
        "customer.view",
        "customer.manage",
        "technician.view",
        "booking.view",
        "booking.manage",
        "assignment.manage",
        "quotation.manage",
        "invoice.manage",
        "payment.manage",
        "tracking.view",
        "gst.view",
        "report.view",
    ],
    UserRole.TECHNICIAN.value: ["booking.view", "quotation.manage", "tracking.view", "tracking.update"],
    UserRole.CUSTOMER.value: ["booking.view"],
    UserRole.ACCOUNTANT.value: ["invoice.manage", "payment.manage", "gst.view", "report.view", "report.export"],
    UserRole.INVENTORY_MANAGER.value: ["report.view"],
}


def _serialize_user(user: User):
    return {
        "id": str(user.id),
        "name": user.name,
        "mobile": user.mobile,
        "email": user.email,
        "role": user.role.value,
        "city": user.city,
        "is_verified": user.is_verified,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


def _normalize_role(role_code: str) -> UserRole:
    try:
        return UserRole(role_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unsupported role '{role_code}'") from exc


def _ensure_internal_role(role_code: str):
    if role_code not in MANAGED_INTERNAL_ROLES:
        raise HTTPException(
            status_code=400,
            detail="This role must be managed through its dedicated module (customers or technicians)",
        )


async def _get_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _ensure_unique_user_fields(
    db: AsyncSession,
    mobile: str | None = None,
    email: str | None = None,
    exclude_user_id: UUID | None = None,
):
    conditions = []
    if mobile:
        conditions.append(User.mobile == mobile)
    if email:
        conditions.append(User.email == email)
    if not conditions:
        return
    query = select(User).where(or_(*conditions))
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    existing = (await db.execute(query)).scalars().all()
    for item in existing:
        if mobile and item.mobile == mobile:
            raise HTTPException(status_code=400, detail="Mobile number already exists")
        if email and item.email == email:
            raise HTTPException(status_code=400, detail="Email already exists")


async def _ensure_rbac_seeded(db: AsyncSession):
    roles = {item.code: item for item in (await db.execute(select(Role))).scalars().all()}
    for code, metadata in ROLE_METADATA.items():
        if code not in roles:
            role = Role(code=code, name=metadata["name"], description=metadata["description"], is_system=True)
            db.add(role)
            roles[code] = role
    permissions = {item.code: item for item in (await db.execute(select(Permission))).scalars().all()}
    for metadata in PERMISSION_CATALOG:
        if metadata["code"] not in permissions:
            permission = Permission(**metadata)
            db.add(permission)
            permissions[metadata["code"]] = permission
    await db.flush()

    for role_code, permission_codes in DEFAULT_ROLE_PERMISSIONS.items():
        role = roles[role_code]
        existing_count = (
            await db.execute(
                select(func.count()).select_from(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.is_active == True,
                )
            )
        ).scalar_one()
        if existing_count > 0:
            continue
        for permission_code in permission_codes:
            permission = permissions.get(permission_code)
            if permission:
                db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    await db.flush()


async def _collect_permissions(db: AsyncSession, user: User):
    role = (await db.execute(select(Role).where(Role.code == user.role.value, Role.is_active == True))).scalar_one_or_none()
    role_permissions = []
    if role:
        role_permissions = (
            await db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(
                    RolePermission.role_id == role.id,
                    RolePermission.is_active == True,
                    Permission.is_active == True,
                )
            )
        ).scalars().all()

    overrides = (
        await db.execute(
            select(Permission.code, UserPermission.is_granted)
            .join(UserPermission, UserPermission.permission_id == Permission.id)
            .where(
                UserPermission.user_id == user.id,
                UserPermission.is_active == True,
                Permission.is_active == True,
            )
        )
    ).all()
    effective = set(role_permissions)
    override_payload = []
    for permission_code, is_granted in overrides:
        override_payload.append({"permission_code": permission_code, "is_granted": is_granted})
        if is_granted:
            effective.add(permission_code)
        else:
            effective.discard(permission_code)
    return {
        "role_permissions": sorted(role_permissions),
        "overrides": sorted(override_payload, key=lambda item: item["permission_code"]),
        "effective_permissions": sorted(effective),
    }


@router.get("/roles", summary="List roles [Admin]")
async def list_roles(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _ensure_rbac_seeded(db)
    roles = (await db.execute(select(Role).where(Role.is_active == True).order_by(Role.code.asc()))).scalars().all()
    return success_response(
        data=[
            {
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "managed_via": "users" if role.code in MANAGED_INTERNAL_ROLES else ("technicians" if role.code == UserRole.TECHNICIAN.value else "customers"),
            }
            for role in roles
        ]
    )


@router.get("/permissions", summary="List permissions [Admin]")
async def list_permissions(current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    await _ensure_rbac_seeded(db)
    permissions = (
        await db.execute(select(Permission).where(Permission.is_active == True).order_by(Permission.module.asc(), Permission.code.asc()))
    ).scalars().all()
    return success_response(
        data=[
            {
                "code": permission.code,
                "module": permission.module,
                "name": permission.name,
                "description": permission.description,
            }
            for permission in permissions
        ]
    )


@router.get("/roles/{role_code}/permissions", summary="Role permissions [Admin]")
async def get_role_permissions(
    role_code: str,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_rbac_seeded(db)
    role = (await db.execute(select(Role).where(Role.code == role_code, Role.is_active == True))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    permissions = (
        await db.execute(
            select(Permission.code, Permission.module, Permission.name)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(
                RolePermission.role_id == role.id,
                RolePermission.is_active == True,
                Permission.is_active == True,
            )
            .order_by(Permission.module.asc(), Permission.code.asc())
        )
    ).all()
    return success_response(
        data={
            "role": {"code": role.code, "name": role.name},
            "permissions": [{"code": code, "module": module, "name": name} for code, module, name in permissions],
        }
    )


@router.put("/roles/{role_code}/permissions", summary="Update role permissions [Super Admin]")
async def update_role_permissions(
    role_code: str,
    payload: UpdateRolePermissionsRequest,
    current_user: dict = Depends(SuperAdminOnly),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_rbac_seeded(db)
    role = (await db.execute(select(Role).where(Role.code == role_code, Role.is_active == True))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    unique_codes = list(dict.fromkeys(payload.permission_codes))
    permissions = (
        await db.execute(select(Permission).where(Permission.code.in_(unique_codes), Permission.is_active == True))
    ).scalars().all()
    if len(permissions) != len(unique_codes):
        raise HTTPException(status_code=400, detail="One or more permission codes are invalid")
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    for permission in permissions:
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    await db.commit()
    return success_response(message="Role permissions updated successfully")


@router.get("", summary="List users [Admin]")
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: str | None = Query(None),
    search: str | None = Query(None),
    include_inactive: bool = Query(False),
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    query = select(User)
    if role:
        query = query.where(User.role == _normalize_role(role))
    if search:
        search_term = f"%{search.strip()}%"
        query = query.where(
            or_(
                User.name.ilike(search_term),
                User.mobile.ilike(search_term),
                User.email.ilike(search_term),
            )
        )
    if not include_inactive:
        query = query.where(User.is_active == True)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    users = (await db.execute(query.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return success_response(
        data={
            "items": [_serialize_user(user) for user in users],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
    )


@router.post("", summary="Create internal user [Admin]")
async def create_user(
    payload: CreateInternalUserRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    role = _normalize_role(payload.role)
    _ensure_internal_role(role.value)
    await _ensure_unique_user_fields(db, mobile=payload.mobile, email=payload.email)
    user = User(
        name=payload.name,
        mobile=payload.mobile,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=role,
        city=payload.city,
        is_verified=True,
    )
    db.add(user)
    await db.flush()
    await db.commit()
    return success_response(data=_serialize_user(user), message="User created successfully")


@router.get("/{user_id}/permissions", summary="User permissions [Admin]")
async def get_user_permissions(
    user_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_rbac_seeded(db)
    user = await _get_user_or_404(db, user_id)
    permissions = await _collect_permissions(db, user)
    return success_response(data={"user_id": str(user.id), "role": user.role.value, **permissions})


@router.put("/{user_id}/permissions", summary="Update user permission overrides [Admin]")
async def update_user_permissions(
    user_id: UUID,
    payload: UpdateUserPermissionsRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_rbac_seeded(db)
    user = await _get_user_or_404(db, user_id)
    unique_codes = list(dict.fromkeys(item.permission_code for item in payload.overrides))
    permissions = (
        await db.execute(select(Permission).where(Permission.code.in_(unique_codes), Permission.is_active == True))
    ).scalars().all()
    permission_map = {item.code: item for item in permissions}
    if len(permission_map) != len(unique_codes):
        raise HTTPException(status_code=400, detail="One or more permission codes are invalid")
    await db.execute(delete(UserPermission).where(UserPermission.user_id == user.id))
    for item in payload.overrides:
        permission = permission_map[item.permission_code]
        db.add(UserPermission(user_id=user.id, permission_id=permission.id, is_granted=item.is_granted))
    await db.commit()
    return success_response(message="User permissions updated successfully")


@router.get("/{user_id}", summary="Get user details [Admin]")
async def get_user(
    user_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_rbac_seeded(db)
    user = await _get_user_or_404(db, user_id)
    permissions = await _collect_permissions(db, user)
    return success_response(data={**_serialize_user(user), **permissions})


@router.put("/{user_id}", summary="Update user [Admin]")
async def update_user(
    user_id: UUID,
    payload: UpdateInternalUserRequest,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(db, user_id)
    if payload.role:
        role = _normalize_role(payload.role)
        _ensure_internal_role(role.value)
        user.role = role
    if payload.mobile and payload.mobile != user.mobile:
        await _ensure_unique_user_fields(db, mobile=payload.mobile, exclude_user_id=user.id)
        user.mobile = payload.mobile
    if payload.email is not None and payload.email != user.email:
        await _ensure_unique_user_fields(db, email=payload.email, exclude_user_id=user.id)
        user.email = payload.email
    if payload.name is not None:
        user.name = payload.name
    if payload.password:
        user.password_hash = hash_password(payload.password)
    if payload.city is not None:
        user.city = payload.city
    if payload.is_verified is not None:
        user.is_verified = payload.is_verified
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await db.commit()
    return success_response(data=_serialize_user(user), message="User updated successfully")


@router.delete("/{user_id}", summary="Deactivate user [Admin]")
async def deactivate_user(
    user_id: UUID,
    current_user: dict = Depends(AdminOnly),
    db: AsyncSession = Depends(get_db),
):
    if current_user["user_id"] == str(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account")
    user = await _get_user_or_404(db, user_id)
    user.is_active = False
    await db.commit()
    return success_response(message="User deactivated successfully")
