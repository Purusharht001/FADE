import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create(db: AsyncSession, *, email: str, hashed_password: str, full_name: str) -> User:
    user = User(
        email=email, hashed_password=hashed_password, full_name=full_name, patients_created=[]
    )
    db.add(user)
    await db.flush()
    return user
