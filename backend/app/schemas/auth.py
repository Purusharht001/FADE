import uuid

from pydantic import EmailStr, Field

from app.models.enums import UserRole
from app.schemas.base import CamelModel


class UserRegister(CamelModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserLogin(CamelModel):
    email: EmailStr
    password: str


class UserRead(CamelModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool


class TokenPair(CamelModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(CamelModel):
    refresh_token: str
