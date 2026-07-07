from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app.core.database import get_db
from app.core.security import decode_token

# auto_error=False so missing/malformed Authorization header returns 401, not 422
bearer_scheme = HTTPBearer(auto_error=False)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials)
        user_id: str = payload.get("sub")
        role: str = payload.get("role")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired or invalid")

    # ── Suspended / terminated user check ─────────────────────────────────────
    # Verify the account is still active on every request. This means suspending
    # a user in the admin dashboard immediately blocks all their active sessions —
    # they cannot make any further API calls even with a valid JWT.
    from uuid import UUID as _UUID
    from sqlalchemy import select as _select
    from app.models.user import User as _User
    user_row = (await db.execute(_select(_User).where(_User.id == _UUID(user_id)))).scalar_one_or_none()
    if not user_row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user_row.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Please contact an administrator.",
        )
    return {"user_id": user_id, "role": role}

def require_roles(*roles: str):
    async def checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        return current_user
    return checker

# Shortcut role guards
AdminOnly       = require_roles("SUPER_ADMIN", "ADMIN")
AdminOrCCO      = require_roles("SUPER_ADMIN", "ADMIN", "CCO")
AdminOrTech     = require_roles("SUPER_ADMIN", "ADMIN", "TECHNICIAN")
AdminCCOTech    = require_roles("SUPER_ADMIN", "ADMIN", "CCO", "TECHNICIAN")
AnyStaff        = require_roles("SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT", "INVENTORY_MANAGER")
AnyAuthenticated = require_roles("SUPER_ADMIN","ADMIN","CCO","TECHNICIAN","CUSTOMER","ACCOUNTANT","INVENTORY_MANAGER")
TechnicianOnly  = require_roles("TECHNICIAN")
