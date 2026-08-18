from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    app_error_handler,
    invalid_token_handler,
    unhandled_exception_handler,
)
from app.core.logging import configure_logging, get_logger
from app.core.security import InvalidTokenError
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.middleware.logging import RequestLoggingMiddleware

configure_logging()
logger = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.is_sqlite:
        # Zero-config local/dev/demo path: create tables directly. Postgres
        # deployments should run `alembic upgrade head` instead (see
        # backend/README.md) so schema changes are tracked as migrations.
        import app.models  # noqa: F401 - registers models on Base.metadata

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_ready", mode="sqlite-autocreate")
    else:
        logger.info("database_ready", mode="managed-by-alembic")

    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "FADE backend — MRI preprocessing/volumetry pipeline, Mamdani fuzzy inference engine, "
            "and the clinician-facing REST API behind the triage dashboard."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # add_exception_handler's declared type wants Callable[[Request, Exception], ...];
    # FastAPI dispatches by the registered exception *subclass* at runtime regardless
    # of the handler's own narrower parameter type, so these are safe — a well-known
    # false positive against Starlette's stub, not a real type error.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(InvalidTokenError, invalid_token_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
