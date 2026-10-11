"""SQLAlchemy ORM models.

Imported eagerly so every mapped class is registered on `Base.metadata`
before Alembic autogeneration or `create_all()` runs.
"""

from app.models.audit import AuditLog
from app.models.patient import Patient
from app.models.scan import BiomarkerReading, FISResult, Scan
from app.models.study import ClinicianRating
from app.models.user import User

__all__ = [
    "User",
    "Patient",
    "Scan",
    "BiomarkerReading",
    "FISResult",
    "ClinicianRating",
    "AuditLog",
]
