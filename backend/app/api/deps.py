import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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
        if get_settings().auth_mode != "demo":
            raise UnauthorizedError("Authentication is required. Send a bearer token.")
        # AUTH_MODE=demo: a request with no token acts as one shared demo
        # clinician, so the dashboard works before a login screen exists.
        # Settings refuse this mode in production. A request that does send
        # a token is authenticated as that user in either mode, and role
        # checks apply in both.
        return await user_repo.get_or_create_default(db)

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
