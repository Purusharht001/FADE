from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.core.security import InvalidTokenError


class AppError(Exception):
    """Base class for domain errors that should map to a clean HTTP response
    instead of leaking a stack trace to the client.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class UnprocessableScanError(AppError):
    """Raised when the preprocessing/volumetry pipeline can't extract
    reliable biomarkers from an uploaded scan (e.g. failed skull-strip,
    corrupt NIfTI header) — surfaced distinctly so a clinician sees
    'this scan needs manual handling', not a generic 500.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "unprocessable_scan"


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.message)


async def invalid_token_handler(request: Request, exc: InvalidTokenError) -> JSONResponse:
    return _error_response(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "An unexpected error occurred."
    )
