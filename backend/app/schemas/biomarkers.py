"""Request models for biomarkers measured outside this service."""

import datetime
from typing import Annotated

from pydantic import AfterValidator, Field, model_validator

from app.core.identifiers import validate_external_subject_id
from app.models.enums import BiomarkerKey
from app.schemas.base import CamelModel

ExternalSubjectId = Annotated[str | None, AfterValidator(validate_external_subject_id)]

PositiveVolume = Annotated[float, Field(gt=0, description="mm3")]


class RawVolumesIn(CamelModel):
    """Raw FreeSurfer-style volumes in mm3, from which the three engine
    inputs are derived server-side with the same formulas the stats parser
    path uses.
    """

    icv_mm3: PositiveVolume
    brain_seg_vol_mm3: PositiveVolume
    left_hippocampus_mm3: PositiveVolume
    right_hippocampus_mm3: PositiveVolume
    left_lateral_ventricle_mm3: PositiveVolume
    right_lateral_ventricle_mm3: PositiveVolume
    left_inf_lat_vent_mm3: PositiveVolume
    right_inf_lat_vent_mm3: PositiveVolume
    third_ventricle_mm3: float | None = Field(default=None, ge=0)
    fourth_ventricle_mm3: float | None = Field(default=None, ge=0)
    left_cerebral_cortex_mm3: float | None = Field(default=None, gt=0)
    right_cerebral_cortex_mm3: float | None = Field(default=None, gt=0)
    # Mean cortical thickness over the configured regions. Omit it if it was
    # not measured; the result is then indeterminate.
    cortical_thickness_mm: float | None = Field(default=None, gt=0)


class DerivedValueIn(CamelModel):
    value: float
    unit: str = Field(min_length=1, max_length=16)


class DerivedBiomarkersIn(CamelModel):
    """The three engine inputs, each with its unit stated explicitly:
    hippocampal volume in '‰ICV' (or 'permille_ICV'), ventricle-to-brain
    ratio in 'ratio', cortical thickness in 'mm'. Any other unit is
    rejected rather than converted (the units are checked by
    `biomarker_derivation.canonical_unit`, not here). Omit a biomarker that
    was not measured.
    """

    hippocampal_volume: DerivedValueIn | None = None
    ventricle_brain_ratio: DerivedValueIn | None = None
    cortical_thickness: DerivedValueIn | None = None

    @model_validator(mode="after")
    def _check_values(self) -> "DerivedBiomarkersIn":
        supplied = {key: self.entry_of(key) for key in BiomarkerKey}
        if all(entry is None for entry in supplied.values()):
            raise ValueError("Supply at least one of the three biomarkers.")
        for key, entry in supplied.items():
            if entry is None:
                continue
            if entry.value <= 0 and key != BiomarkerKey.VENTRICLE_BRAIN_RATIO:
                raise ValueError(f"{key.value} must be greater than zero.")
            if entry.value < 0:
                raise ValueError(f"{key.value} must not be negative.")
        return self

    def entry_of(self, key: BiomarkerKey) -> DerivedValueIn | None:
        entry: DerivedValueIn | None = getattr(self, key.value)
        return entry


class ManualBiomarkersRequest(CamelModel):
    """Either `rawVolumes` or `derived`, never both."""

    raw_volumes: RawVolumesIn | None = None
    derived: DerivedBiomarkersIn | None = None
    external_subject_id: ExternalSubjectId = None
    scan_date: datetime.date | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "ManualBiomarkersRequest":
        if (self.raw_volumes is None) == (self.derived is None):
            raise ValueError("Supply exactly one of rawVolumes or derived.")
        return self
