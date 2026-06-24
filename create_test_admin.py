"""
Creates/resets admin users for testing.
Run: python create_test_admin.py
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.core.database import engine
from app.core.security import hash_password
from app.models.user import User

USERS = [
    {"name": "Admin User",  "email": "admin@palei.in",            "mobile": "9000000000", "password": "testing"},
    {"name": "Super Admin", "email": "admin@paleisolutions.com",   "mobile": "9999999999", "password": "testing"},
]

async def run():
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        for u_data in USERS:
            r = await db.execute(select(User).where(User.email == u_data["email"]))
            u = r.scalar_one_or_none()
            if u:
                u.password_hash = hash_password(u_data["password"])
                u.role = "SUPER_ADMIN"; u.is_active = True
                print(f"Updated {u_data['email']}")
            else:
                db.add(User(
                    name=u_data["name"], email=u_data["email"],
                    mobile=u_data["mobile"],
                    password_hash=hash_password(u_data["password"]),
                    role="SUPER_ADMIN", is_active=True,
                ))
                print(f"Created {u_data['email']}")
        await db.commit()
    await engine.dispose()

    print("\nLogin credentials (password: testing):")
    for u in USERS:
        print(f"  {u['email']}  /  testing")

asyncio.run(run())
