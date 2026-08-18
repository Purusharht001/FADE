"""Orchestrates the full scan -> biomarkers -> stage pipeline (Phase 5).

Two entry points:
- `run_pipeline_from_file`: the real path — load a NIfTI file, preprocess,
  segment, extract biomarkers, run fuzzy inference.
- `run_pipeline_synthetic`: generates a synthetic scan in-memory (or on
  disk, optionally) and pushes it through the exact same preprocessing /
  volumetry / inference code — used by the demo seed script and by the
  `/fis/simulate` endpoint's "generate a case" mode, so the demo data is
  produced by the real pipeline rather than hand-rolled numbers.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.core.exceptions import UnprocessableScanError
from app.models.enums import BiomarkerKey
from app.services import preprocessing, synthetic_mri, volumetry
from app.services.biomarkers import BIOMARKER_DEFS
from app.services.fis_engine import FISOutput, run_fis


@dataclass(slots=True)
class PipelineResult:
    biomarkers: dict[BiomarkerKey, float]
    abnormality: dict[BiomarkerKey, float]
    fis_output: FISOutput


def _run_from_preprocessed(volume: preprocessing.PreprocessedVolume) -> PipelineResult:
    biomarkers = volumetry.run_volumetry(volume)
    abnormality = {key: BIOMARKER_DEFS[key].abnormality(value) for key, value in biomarkers.items()}
    settings = get_settings()
    fis_output = run_fis(biomarkers, review_threshold=settings.uncertainty_review_threshold)
    return PipelineResult(biomarkers=biomarkers, abnormality=abnormality, fis_output=fis_output)


def run_pipeline_from_file(path: str | Path) -> PipelineResult:
    volume = preprocessing.preprocess_file(path)
    return _run_from_preprocessed(volume)


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
        return _run_from_preprocessed(volume)
    except UnprocessableScanError:
        # Extremely low/high severities can occasionally under-fill a tissue
        # band enough that multi-Otsu can't split 4 classes; retry once with
        # a touch of severity jitter rather than surfacing a flaky failure.
        intensity, _labels, _truth = synthetic_mri.generate_synthetic_scan(
            min(max(severity + 0.03, 0.0), 1.0), seed=(seed or 0) + 1
        )
        volume = preprocessing.preprocess(intensity, affine)
        return _run_from_preprocessed(volume)
