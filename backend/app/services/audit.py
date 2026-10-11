"""The audit log: one entry per sign-off, rating, export and stats
attachment.

An audit log is only safe to keep, share and back up if it cannot contain
patient information. `record` enforces that mechanically instead of by
convention: every value in an entry's `detail` must be a number, a
boolean, null, or a short token made of letters, digits and `_ . : -`
(an identifier, an enum value, a version string). A sentence, a name or a
date of birth does not fit that shape and is refused with an error, so a
future call site cannot quietly start logging free text.
"""

import re
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User

# Actions. Kept here so the set of audited events is visible in one place.
ACTION_SCAN_REVIEW = "scan.review"
ACTION_RATING_CREATE = "rating.create"
ACTION_RATING_UPDATE = "rating.update"
ACTION_EXPORT_RESULTS = "export.results_csv"
ACTION_STATS_ATTACHED = "scan.stats_attached"
ACTION_COHORT_IMPORT = "cohort.import"

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{0,64}$")
_MAX_LIST_LENGTH = 32


class UnsafeAuditDetailError(ValueError):
    """An audit entry was about to carry something that is not provably
    free of patient information. A programming error, not a user error.
    """


def _check_value(key: str, value: Any) -> None:
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if _SAFE_TOKEN.fullmatch(value):
            return
        raise UnsafeAuditDetailError(
            f"Audit detail '{key}' is free text; only identifiers and codes may be logged."
        )
    if isinstance(value, list | tuple):
        if len(value) > _MAX_LIST_LENGTH:
            raise UnsafeAuditDetailError(f"Audit detail '{key}' is too long a list to log.")
        for item in value:
            if isinstance(item, list | tuple | dict):
                raise UnsafeAuditDetailError(f"Audit detail '{key}' must be a flat list.")
            _check_value(key, item)
        return
    raise UnsafeAuditDetailError(
        f"Audit detail '{key}' has unsupported type {type(value).__name__}."
    )


def validate_detail(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Returns `detail` as a plain dict, or raises UnsafeAuditDetailError.
    The error names the offending key and never repeats its value.
    """
    if detail is None:
        return None
    clean: dict[str, Any] = {}
    for key, value in detail.items():
        if not isinstance(key, str) or not _SAFE_TOKEN.fullmatch(key):
            raise UnsafeAuditDetailError("Audit detail keys must be short code-like strings.")
        _check_value(key, value)
        clean[key] = list(value) if isinstance(value, tuple) else value
    return clean


async def record(
    db: AsyncSession,
    *,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Adds an audit entry to the current transaction, so it is committed
    with the action it describes and rolled back with it too.
    """
    entry = AuditLog(
        actor_id=actor.id if actor is not None else None,
        actor_role=actor.role.value if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=validate_detail(detail),
    )
    db.add(entry)
    await db.flush()
    return entry
