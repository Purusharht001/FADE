"""Checks run once while the application starts.

Each either refuses to start (a configuration that would expose data) or
logs one clear line about the security posture the server is running in.
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DEMO_MODE_WARNING = (
    "AUTH_MODE=demo: any request without a bearer token acts as the shared demo clinician. "
    "Anyone who can reach this server can read and change every record. Do not load real "
    "patient data in this mode; set AUTH_MODE=required."
)


class UnsafeConfigurationError(RuntimeError):
    """The server is configured in a way that would expose stored scans."""


def _static_directories(app: FastAPI) -> list[Path]:
    directories: list[Path] = []
    for route in app.routes:
        if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
            directories += [Path(d).resolve() for d in route.app.all_directories]
    return directories


def ensure_upload_dir_not_served(app: FastAPI, settings: Settings) -> None:
    """Refuses to start if the uploads directory is, contains, or sits
    inside a directory the app serves as static files. Stored scans are
    reachable only through the API, never by URL.
    """
    upload_dir = Path(settings.upload_dir).resolve()
    for served in _static_directories(app):
        if upload_dir == served or served in upload_dir.parents or upload_dir in served.parents:
            raise UnsafeConfigurationError(
                f"UPLOAD_DIR ({upload_dir}) overlaps the static directory {served}. Uploaded "
                "scans must not be served as static files; move UPLOAD_DIR elsewhere."
            )


def log_security_posture(settings: Settings) -> None:
    """One line at startup saying how requests are authenticated."""
    if settings.auth_mode == "demo":
        logger.warning("auth_demo_mode", detail=DEMO_MODE_WARNING)
    else:
        logger.info(
            "auth_required",
            self_registration=bool(settings.allow_self_registration),
        )
