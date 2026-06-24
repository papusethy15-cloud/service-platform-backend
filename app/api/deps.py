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
        return {"user_id": user_id, "role": role}
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired or invalid")

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
