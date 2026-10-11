"""Orchestrates the full scan -> biomarkers -> stage pipeline (Phase 5).

Entry points:
- `run_pipeline_from_file`: the in-house path. Load a NIfTI file,
  preprocess, segment with the phantom-tuned volumetry, extract
  biomarkers, run fuzzy inference.
- `run_pipeline_synthetic`: generates a synthetic scan in-memory (or on
  disk, optionally) and pushes it through the exact same preprocessing /
  volumetry / inference code. Used by the demo seed script and the
  `/scans/synthetic` endpoint, so the demo data is produced by the real
  pipeline rather than hand-rolled numbers.

Both end in `evaluate_biomarkers`, the single place where measured values
meet the calibration and the engine.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey
from app.services import preprocessing, synthetic_mri, volumetry
from app.services.calibration import Calibration, DataDomain, get_calibration
from app.services.fis_engine import FISOutput, calibration_mismatch_reasons, run_fis


@dataclass(slots=True)
class PipelineResult:
    # Only the biomarkers that were actually measured. A missing one is
    # absent here and makes `fis_output` indeterminate.
    biomarkers: dict[BiomarkerKey, float]
    # None where the active calibration does not apply to the value, so no
    # severity bar is drawn from breakpoints fitted to something else.
    abnormality: dict[BiomarkerKey, float | None]
    fis_output: FISOutput
    units: dict[BiomarkerKey, str] = field(default_factory=dict)


def abnormality_for(
    calibration: Calibration,
    key: BiomarkerKey,
    value: float,
    unit: str,
    data_domain: DataDomain | None,
) -> float | None:
    """A biomarker's 0-1 abnormality under `calibration`, or None when the
    calibration cannot be applied to this value (incomplete, fitted to the
    other data domain, or expressed in a different unit).
    """
    if calibration_mismatch_reasons(calibration, data_domain):
        return None
    if unit != calibration.units[key]:
        return None
    return calibration.biomarkers[key].abnormality(value)


def evaluate_biomarkers(
    values: Mapping[BiomarkerKey, float | None],
    units: Mapping[BiomarkerKey, str],
    *,
    data_domain: DataDomain | None,
    qc_flags: Sequence[Mapping[str, Any]] = (),
    calibration: Calibration | None = None,
) -> PipelineResult:
    """Scores measured biomarker values against the active calibration and
    runs the fuzzy engine on them.
    """
    calibration = calibration or get_calibration()
    settings = get_settings()

    present = {key: float(value) for key, value in values.items() if value is not None}
    present_units = {key: units[key] for key in present}
    abnormality = {
        key: abnormality_for(calibration, key, value, present_units[key], data_domain)
        for key, value in present.items()
    }
    fis_output = run_fis(
        values,
        review_threshold=settings.uncertainty_review_threshold,
        calibration=calibration,
        data_domain=data_domain,
        units=present_units,
        qc_flags=qc_flags,
    )
    return PipelineResult(
        biomarkers=present, abnormality=abnormality, fis_output=fis_output, units=present_units
    )


def _run_from_preprocessed(
    volume: preprocessing.PreprocessedVolume, data_domain: DataDomain
) -> PipelineResult:
    biomarkers = volumetry.run_volumetry(volume)
    return evaluate_biomarkers(biomarkers, volumetry.INHOUSE_UNITS, data_domain=data_domain)


def run_pipeline_from_file(
    path: str | Path, *, data_domain: DataDomain = DataDomain.PHANTOM
) -> PipelineResult:
    """The in-house path. `data_domain` is the domain of the patient the
    file belongs to: the volumetry here is tuned to the phantom, so a file
    for any other patient is scored as real data and comes back
    indeterminate under a phantom calibration.
    """
    volume = preprocessing.preprocess_file(path)
    return _run_from_preprocessed(volume, data_domain)


def run_pipeline_synthetic(
    severity: float, *, seed: int | None = None, save_to: str | Path | None = None
) -> PipelineResult:
    intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(severity, seed=seed)
    if save_to is not None:
        synthetic_mri.save_nifti(intensity, save_to)

    affine = np.diag(
        [synthetic_mri.VOXEL_SIZE_MM, synthetic_mri.VOXEL_SIZE_MM, synthetic_mri.VOXEL_SIZE_MM, 1.0]
    )
    try:
        volume = preprocessing.preprocess(intensity, affine)
        return _run_from_preprocessed(volume, DataDomain.PHANTOM)
    except UnprocessableScanError:
        # Extremely low/high severities can occasionally under-fill a tissue
        # band enough that multi-Otsu can't split 4 classes; retry once with
        # a touch of severity jitter rather than surfacing a flaky failure.
        intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(
            min(max(severity + 0.03, 0.0), 1.0), seed=(seed or 0) + 1
        )
        volume = preprocessing.preprocess(intensity, affine)
        return _run_from_preprocessed(volume, DataDomain.PHANTOM)
