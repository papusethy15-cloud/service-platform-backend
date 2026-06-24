"""
Run once to create the admin user:
  cd backend && python seed_admin.py
"""
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.core.config import settings
from app.core.security import hash_password
from app.models.user import User

async def seed():
    engine = create_async_engine(settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Check if admin already exists
        result = await session.execute(select(User).where(User.email == "admin@bibekenterprises.com"))
        existing = result.scalar_one_or_none()

        if existing:
            # Update password in case it changed
            existing.password_hash = hash_password("Mithun@15")
            existing.role = "SUPER_ADMIN"
            existing.is_active = True
            await session.commit()
            print(f"✅ Admin user updated: admin@bibekenterprises.com")
        else:
            admin = User(
                name="Super Admin",
                email="admin@bibekenterprises.com",
                mobile="9999999999",
                password_hash=hash_password("Mithun@15"),
                role="SUPER_ADMIN",
                is_active=True,
            )
            session.add(admin)
            await session.commit()
            print(f"✅ Admin user created: admin@bibekenterprises.com / Mithun@15")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(seed())
