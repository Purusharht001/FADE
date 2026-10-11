"""Schemas for the blind clinician-rating study.

Everything a rater receives is defined here, and it is deliberately
little: the measured biomarkers and the patient's age and sex. No engine
output (stage, confidence, uncertainty, severity, abnormality, fired
rules, QC flags), no reference label, and no identifier that could be
looked up elsewhere (subject id, display id, data source, scan date).
"""

import datetime
import uuid

from pydantic import Field

from app.models.enums import BiomarkerKey, Sex, Stage
from app.models.study import NOTES_MAX_LENGTH
from app.schemas.base import CamelModel


class StudyBiomarker(CamelModel):
    key: BiomarkerKey
    value: float
    unit: str


class RatingIn(CamelModel):
    stage_called: Stage
    borderline: bool = False
    confidence: int = Field(ge=1, le=5, description="1 = guessing, 5 = certain")
    notes: str | None = Field(
        default=None,
        max_length=NOTES_MAX_LENGTH,
        description="Optional reasoning. Do not enter names, dates or other identifying details.",
    )


class RatingRead(CamelModel):
    scan_id: uuid.UUID
    stage_called: Stage
    borderline: bool
    confidence: int
    notes: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class StudyCase(CamelModel):
    scan_id: uuid.UUID
    # A short label for talking about a case; derived from the scan id and
    # carrying no information about the patient or the result.
    case_code: str
    age: int
    sex: Sex
    biomarkers: list[StudyBiomarker]
    # The requesting rater's own rating, if they have already given one.
    my_rating: RatingRead | None = None
