"""SQLAlchemy ORM models.

Imported eagerly so every mapped class is registered on `Base.metadata`
before Alembic autogeneration or `create_all()` runs.
"""

from app.models.patient import Patient
from app.models.scan import BiomarkerReading, FISResult, Scan
from app.models.user import User

__all__ = ["User", "Patient", "Scan", "BiomarkerReading", "FISResult"]
