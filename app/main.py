from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.router import api_router


async def _auto_migrate():
    """Auto-apply any missing DB columns/tables on startup using SQLAlchemy create_all (safe, additive-only)."""
    try:
        from app.core.database import engine
        from app.models.base import Base
        # Import all models so metadata is populated
        import app.models.user
        import app.models.customer
        import app.models.technician
        import app.models.booking
        import app.models.service
        import app.models.invoice
        import app.models.payment
        import app.models.quotation
        import app.models.commission
        import app.models.wallet
        import app.models.domain
        import app.models.callback_request
        import app.models.tracking
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("[OK] Auto-migrate: DB schema up-to-date")
    except Exception as e:
        print(f"[WARN] Auto-migrate skipped: {e}")


async def _seed_admin():
    """Create the default super-admin user if it doesn't exist."""
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.user import User
        from app.core.security import hash_password
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.email == "admin@paleisolutions.com")
            )
            if not result.scalar_one_or_none():
                admin = User(
                    name="Super Admin",
                    email="admin@paleisolutions.com",
                    mobile="9999999999",
                    password_hash=hash_password("Srikanta@15"),
                    role="SUPER_ADMIN",
                    is_active=True,
                    is_verified=True,
                )
                session.add(admin)
                await session.commit()
                print("[OK] Admin seeded: admin@paleisolutions.com / Srikanta@15")
            else:
                print("[OK] Admin already exists")
    except Exception as e:
        print(f"[WARN] Admin seed skipped: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ─────────────────────────────────────────────
    await _auto_migrate()
    await _seed_admin()
    yield
    # ── shutdown ─────────────────────────────────────────────


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
def health_check():
    return {"status": "ok", "app": settings.APP_NAME}
