import os
from collections.abc import AsyncGenerator

# The suite's expected values are written against the phantom calibration.
# Pin it before the app reads settings, so a developer's backend/.env that
# selects a real calibration (CALIBRATION_FILE=real.v1.json) does not turn
# into dozens of spurious failures. Tests about other calibrations load
# their files explicitly.
os.environ["CALIBRATION_FILE"] = "phantom.v0.json"
os.environ["VOLUMETRY_PROVIDER"] = "external"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers every mapped class on Base.metadata
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.enums import UserRole
from app.repositories import user_repo

TEST_DATABASE_URL = "sqlite+aiosqlite://"  # in-memory, unique per engine instance


@pytest.fixture(autouse=True, scope="session")
def _isolate_upload_dir(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Any test that runs a synthetic scan with a seed (directly via
    scan_service, or indirectly through the `/scans/synthetic` HTTP
    endpoint) saves a real ~1MB .nii.gz via `settings.upload_dir`. Redirect
    that, for the whole test session, into a pytest-managed temp directory
    instead of the real configured upload_dir — otherwise every test run
    silently accumulates files in the working tree's data/uploads/.
    """
    get_settings().upload_dir = str(tmp_path_factory.mktemp("uploads"))


@pytest.fixture(autouse=True)
def _rate_limiting_off():
    """Every test request comes from the same client address, so the
    per-IP limits would start refusing requests partway through the suite.
    They are switched off here and switched back on, with fresh counters,
    by the tests that are about rate limiting (the `rate_limits` fixture).
    """
    limiter.enabled = False
    yield
    limiter.enabled = False


@pytest.fixture
def rate_limits():
    """Rate limiting on, starting from zero."""
    limiter.reset()
    limiter.enabled = True
    yield limiter
    limiter.enabled = False
    limiter.reset()


@pytest_asyncio.fixture
async def app_db() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """The session factory behind the `client` fixture's database. A test
    that needs to arrange or inspect rows the API cannot reach (a scan in
    a particular status, the audit log) requests this alongside `client`.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def client(app_db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncClient]:
    """A fresh in-memory SQLite database per test, wired into the real
    FastAPI app via a dependency override — every test starts from a clean
    schema with no cross-test state leakage.
    """
    session_factory = app_db

    async def _override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    fastapi_app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """A raw session against a fresh in-memory database, for tests that
    exercise service/repository code directly rather than through the HTTP
    API (e.g. scan_service.py, which the API layer only exercises
    indirectly).
    """
    engine = create_async_engine(
        TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def login_as(
    client: AsyncClient,
    app_db: async_sessionmaker[AsyncSession],
    role: UserRole,
    email: str | None = None,
) -> dict[str, str]:
    """Test helper: creates a user with the given role directly in the
    database (registration only ever creates clinicians) and returns auth
    headers for it.
    """
    email = email or f"{role.value}@test.dev"
    password = "testpassword123"  # noqa: S105 - test-only credential
    async with app_db() as session:
        await user_repo.create(
            session,
            email=email,
            hashed_password=hash_password(password),
            full_name=f"Test {role.value}",
            role=role,
        )
        await session.commit()
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


async def register_and_login(client: AsyncClient, email: str = "doc@test.dev") -> dict[str, str]:
    """Test helper: registers a clinician and returns auth headers."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpassword123", "full_name": "Dr Test"},
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "testpassword123"}
    )
    token = resp.json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}
