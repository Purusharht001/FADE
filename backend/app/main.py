from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    app_error_handler,
    invalid_token_handler,
    rate_limit_handler,
    unhandled_exception_handler,
    validation_error_handler,
)
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import DefaultRateLimitMiddleware, limiter
from app.core.security import InvalidTokenError
from app.core.startup_checks import ensure_upload_dir_not_served, log_security_posture
from app.db.schema import ensure_schema
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.middleware.logging import RequestLoggingMiddleware
from app.services.calibration import get_calibration
from app.services.volumetry_providers import fail_interrupted_jobs

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    ensure_upload_dir_not_served(application, settings)
    log_security_posture(settings)

    # Fail fast on a malformed calibration file rather than on the first
    # scan; an incomplete (null) one is allowed and logged.
    calibration = get_calibration()
    log_calibration = logger.info if calibration.complete else logger.warning
    log_calibration(
        "calibration_loaded",
        version=calibration.version,
        domain=calibration.domain.value,
        complete=calibration.complete,
        provisional=calibration.provisional,
        unset_values=len(calibration.missing),
    )

    # An empty SQLite file is created and stamped (the zero-config local
    # path); a database that is behind the models is refused with the
    # command that fixes it. See app/db/schema.py.
    import app.models  # noqa: F401 - registers models on Base.metadata

    try:
        async with get_engine().begin() as conn:
            state = await conn.run_sync(
                lambda sync_conn: ensure_schema(sync_conn, create_if_empty=settings.is_sqlite)
            )
    except SQLAlchemyError:
        if settings.is_sqlite:
            raise
        # A database that is unreachable at startup is reported, not fatal:
        # the server came up without checking before, and still does.
        logger.warning("database_schema_check_skipped", reason="database unreachable")
        state = "unchecked"
    if state == "empty":
        logger.warning("database_empty", detail="Run `alembic upgrade head` to create the schema.")
    logger.info("database_ready", schema=state)

    # Background segmentation jobs do not survive a restart. Anything a
    # previous process left in `segmenting` is marked failed so it can be
    # recovered, instead of appearing to run forever.
    try:
        async with get_session_factory()() as session:
            await fail_interrupted_jobs(session)
    except Exception:  # noqa: BLE001 - e.g. Postgres not migrated yet; must not block startup
        logger.warning("interrupted_job_sweep_skipped")

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
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)  # type: ignore[arg-type]
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(InvalidTokenError, invalid_token_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Middleware runs outermost-last-added. Rate limiting is added first so
    # that it sits inside CORS: a 429 then still carries the CORS headers a
    # browser needs in order to show it as a 429 and not as a network error.
    app.add_middleware(DefaultRateLimitMiddleware)
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
