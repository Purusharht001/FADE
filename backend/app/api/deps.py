import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.repositories import user_repo

DbSession = Annotated[AsyncSession, Depends(get_db)]

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token.")

    payload = decode_token(credentials.credentials, TokenType.ACCESS)
    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise UnauthorizedError("Malformed token subject.") from exc

    user = await user_repo.get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole):
    async def _checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise ForbiddenError(f"Requires one of roles: {', '.join(r.value for r in roles)}")
        return user

    return _checker
