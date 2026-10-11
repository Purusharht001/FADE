"""Create a user account, or change an existing account's role.

    uv run python scripts/create_user.py --email ml@team.example --name "ML Teammate" \\
        --role researcher

`POST /auth/register` only ever creates clinicians, and is closed when
AUTH_MODE=required. This is how researcher and admin accounts, and every
account on a server that requires authentication, are made.

The password is never taken from the command line, where it would land in
shell history and process listings. It is prompted for, or read from the
environment variable named by --password-env.
"""

import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db.session import dispose_engine, get_session_factory
from app.models.enums import UserRole
from app.models.user import User
from app.repositories import user_repo
from app.services import audit

MIN_PASSWORD_LENGTH = 8  # the same floor POST /auth/register applies
ACTION_USER_CREATE = "user.create"
ACTION_USER_ROLE_CHANGE = "user.role_change"

EXIT_OK = 0
EXIT_ERROR = 2


class UserCommandError(Exception):
    pass


async def create_user(
    db: AsyncSession, *, email: str, full_name: str, role: UserRole, password: str
) -> User:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserCommandError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if await user_repo.get_by_email(db, email) is not None:
        raise UserCommandError(
            "An account with this email already exists. Use --set-role to change its role."
        )
    user = await user_repo.create(
        db, email=email, hashed_password=hash_password(password), full_name=full_name, role=role
    )
    await audit.record(
        db,
        actor=None,
        action=ACTION_USER_CREATE,
        entity_type="user",
        entity_id=user.id,
        detail={"role": role.value},
    )
    return user


async def set_role(db: AsyncSession, *, email: str, role: UserRole) -> User:
    user = await user_repo.get_by_email(db, email)
    if user is None:
        raise UserCommandError("No account with this email exists.")
    previous = user.role
    user.role = role
    await db.flush()
    await audit.record(
        db,
        actor=None,
        action=ACTION_USER_ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        detail={"role": role.value, "previous_role": previous.value},
    )
    return user


def read_password(env_var: str | None) -> str:
    if env_var:
        password = os.environ.get(env_var)
        if not password:
            raise UserCommandError(f"The environment variable {env_var} is not set.")
        return password
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise UserCommandError("The two passwords do not match.")
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_user", description="Create a user, or change an existing user's role."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", help="Full name. Required when creating an account.")
    parser.add_argument("--role", required=True, choices=[role.value for role in UserRole])
    parser.add_argument(
        "--set-role",
        action="store_true",
        help="Change the role of an existing account instead of creating one.",
    )
    parser.add_argument(
        "--password-env",
        metavar="VAR",
        help="Read the new password from this environment variable instead of prompting.",
    )
    return parser


async def _run(args: argparse.Namespace) -> str:
    role = UserRole(args.role)
    try:
        async with get_session_factory()() as session:
            if args.set_role:
                user = await set_role(session, email=args.email, role=role)
                await session.commit()
                return f"Role of {user.email} is now {user.role.value}."
            password = read_password(args.password_env)
            user = await create_user(
                session, email=args.email, full_name=args.name, role=role, password=password
            )
            await session.commit()
            return f"Created {user.role.value} account {user.email}."
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.set_role and not args.name:
        parser.error("--name is required when creating an account")

    configure_logging()
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        message = asyncio.run(_run(args))
    except UserCommandError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(message)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
