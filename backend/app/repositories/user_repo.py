import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import User

# The account every request resolves to when no bearer token is presented —
# see get_or_create_default() and app/api/deps.py's get_current_user(). This
# keeps the auth machinery (models, JWT, /auth/register+login) intact and
# usable, without requiring a real login before the app is reachable.
DEFAULT_USER_EMAIL = "demo@fade.local"


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


async def get_or_create_default(db: AsyncSession) -> User:
    """The account unauthenticated requests resolve to (see
    app/api/deps.py's get_current_user()). Its password is random and
    never handed out anywhere — the account exists purely so every
    `created_by_id`/`reviewed_by_id` foreign key still points at a real
    user row, not so anyone can log in as it.
    """
    existing = await get_by_email(db, DEFAULT_USER_EMAIL)
    if existing is not None:
        return existing
    return await create(
        db,
        email=DEFAULT_USER_EMAIL,
        hashed_password=hash_password(uuid.uuid4().hex),
        full_name="Demo User",
    )
