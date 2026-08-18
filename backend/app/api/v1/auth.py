import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.repositories import user_repo
from app.schemas.auth import RefreshRequest, TokenPair, UserLogin, UserRead, UserRegister

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=201)
async def register(payload: UserRegister, db: DbSession) -> UserRead:
    existing = await user_repo.get_by_email(db, payload.email)
    if existing is not None:
        raise ConflictError("An account with this email already exists.")

    user = await user_repo.create(
        db,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: UserLogin, db: DbSession) -> TokenPair:
    user = await user_repo.get_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise UnauthorizedError("Incorrect email or password.")
    if not user.is_active:
        raise UnauthorizedError("This account has been deactivated.")

    return TokenPair(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id, user.role.value),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    token_payload = decode_token(payload.refresh_token, TokenType.REFRESH)
    user = await user_repo.get_by_id(db, uuid.UUID(token_payload.sub))
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive.")

    return TokenPair(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id, user.role.value),
    )


@router.get("/me", response_model=UserRead)
async def me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)
