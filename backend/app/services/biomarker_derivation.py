"""From parsed volumes to the fuzzy engine's three inputs.

Pure functions: no I/O, no database, no calibration. Each documents its
formula and the unit of its result, because the engine refuses values
whose unit differs from the calibration's.

| Engine input           | Formula                                   | Unit   |
|------------------------|-------------------------------------------|--------|
| hippocampal_volume     | (L + R hippocampus) / ICV x 1000          | ‰ICV   |
| ventricle_brain_ratio  | (L + R lateral + L + R inferior lateral   | ratio  |
|                        | ventricles) / BrainSegVol                 |        |
| cortical_thickness     | mean ThickAvg over the configured regions,| mm     |
|                        | both hemispheres                          |        |

Hippocampal volume here is genuinely ICV-normalised, which is what closes
the labelling half of assumption C2: the unit says so and the number is.

Plausibility checks never raise. A value outside its physiological bounds
adds a QC flag of kind "plausibility", which makes the engine return an
indeterminate result instead of staging a measurement that is probably a
segmentation failure. The bounds come from settings, not from this file.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import status

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.models.enums import BiomarkerKey
from app.services.biomarkers import UNIT_MM, UNIT_PERMILLE_ICV, UNIT_RATIO
from app.services.fis_engine import QC_KIND_PLAUSIBILITY
from app.services.freesurfer_stats import AparcStats, AsegStats, ParsedStats, StatsParseError

HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS

QC_KIND_INFO = "info"

# The units the functions below produce. A calibration for real data must
# declare exactly these (see app/calibration/real.template.json).
DERIVED_UNITS: dict[BiomarkerKey, str] = {
    HV: UNIT_PERMILLE_ICV,
    VBR: UNIT_RATIO,
    CT: UNIT_MM,
}

# Accepted spellings of each unit on input, mapped to the canonical one. The
# per-mille sign is awkward to type in a shell or a CSV, so it has one ASCII
# alias; nothing else is accepted.
UNIT_ALIASES: dict[BiomarkerKey, dict[str, str]] = {
    HV: {UNIT_PERMILLE_ICV: UNIT_PERMILLE_ICV, "permille_ICV": UNIT_PERMILLE_ICV},
    VBR: {UNIT_RATIO: UNIT_RATIO},
    CT: {UNIT_MM: UNIT_MM},
}

ThicknessWeighting = Literal["unweighted", "surface_area"]

# How far a supplied derived value may sit from the same value recomputed
# from the raw volumes supplied beside it before the row is flagged. A
# tolerance on arithmetic, not a clinical threshold.
_CONSISTENCY_REL_TOL = 0.01


class UnitError(AppError):
    """A biomarker value was supplied in a unit this pipeline does not use.
    Units are never converted: a wrong unit is a wrong request.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "invalid_unit"


def canonical_unit(key: BiomarkerKey, unit: str) -> str:
    """The canonical spelling of `unit` for `key`, or UnitError."""
    try:
        return UNIT_ALIASES[key][unit]
    except KeyError as exc:
        accepted = ", ".join(f"'{u}'" for u in UNIT_ALIASES[key])
        raise UnitError(f"{key.value} must be given in {accepted}; got '{unit}'.") from exc


@dataclass(frozen=True, slots=True)
class DerivedBiomarkers:
    # None for a biomarker that could not be derived (for example cortical
    # thickness when no aparc files were supplied).
    values: dict[BiomarkerKey, float | None]
    units: dict[BiomarkerKey, str]
    # Stored on Scan.raw_volumes: the measurements as parsed, plus
    # secondary derived quantities the engine does not consume.
    raw_volumes: dict[str, Any]
    qc_flags: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Formulas
# --------------------------------------------------------------------------


def compute_hippocampal_volume_norm(
    left_hippocampus_mm3: float, right_hippocampus_mm3: float, icv_mm3: float
) -> float:
    """Bilateral hippocampal volume as a fraction of intracranial volume,
    in per-mille: (L + R) / ICV x 1000. Unit: ‰ICV.
    """
    return (left_hippocampus_mm3 + right_hippocampus_mm3) / icv_mm3 * 1000.0


def compute_vbr(
    left_lateral_ventricle_mm3: float,
    right_lateral_ventricle_mm3: float,
    left_inf_lat_vent_mm3: float,
    right_inf_lat_vent_mm3: float,
    brain_seg_vol_mm3: float,
) -> float:
    """Ventricle-to-brain ratio: lateral plus inferior lateral ventricles,
    both sides, over BrainSegVol. Unit: unitless ratio (0.04, not 4%).

    TODO(confirm): the team's earlier parser may have used a different
    denominator (BrainSegVolNotVent, eTIV or a supratentorial volume) or
    left out the inferior lateral ventricles. This definition must
    reproduce 0.04143 for OAS1_0001_MR1; tests/test_biomarker_derivation.py
    has a placeholder test that checks it once that subject's aseg.stats is
    available. Change the formula here and nowhere else.
    """
    ventricles = (
        left_lateral_ventricle_mm3
        + right_lateral_ventricle_mm3
        + left_inf_lat_vent_mm3
        + right_inf_lat_vent_mm3
    )
    return ventricles / brain_seg_vol_mm3


def compute_cortical_gm_volume_norm(
    left_cerebral_cortex_mm3: float, right_cerebral_cortex_mm3: float, icv_mm3: float
) -> float:
    """Cortical grey matter volume over ICV, in per-mille: (L + R) / ICV x 1000.
    Not an engine input; stored for the ML team.
    """
    return (left_cerebral_cortex_mm3 + right_cerebral_cortex_mm3) / icv_mm3 * 1000.0


def compute_cortical_thickness(
    lh: AparcStats,
    rh: AparcStats,
    regions: Sequence[str],
    *,
    weighting: ThicknessWeighting = "unweighted",
) -> float:
    """Mean cortical thickness over `regions` in both hemispheres. Unit: mm.

    "unweighted" is the plain mean of the ThickAvg values, one per region
    per hemisphere. "surface_area" weights each region by its SurfArea, so
    large regions count for more.

    TODO(confirm): the plain mean is what was specified. Whether the
    calibration should instead be fitted to an area-weighted mean is the ML
    team's call; switch with CORTICAL_THICKNESS_WEIGHTING, not by editing
    this function.
    """
    if not regions:
        raise ValueError("No cortical thickness regions are configured.")
    missing = [
        f"{stats.hemisphere}.{region}"
        for stats in (lh, rh)
        for region in regions
        if region not in stats.regions
    ]
    if missing:
        raise StatsParseError(
            f"The aparc stats are missing the region(s) {', '.join(missing)} needed for "
            "cortical thickness."
        )

    entries = [stats.regions[region] for stats in (lh, rh) for region in regions]
    if weighting == "surface_area":
        total_area = sum(e.surf_area_mm2 for e in entries)
        if total_area <= 0:
            raise StatsParseError("The aparc stats report zero surface area for the regions used.")
        return sum(e.thick_avg_mm * e.surf_area_mm2 for e in entries) / total_area
    return sum(e.thick_avg_mm for e in entries) / len(entries)


# --------------------------------------------------------------------------
# Plausibility
# --------------------------------------------------------------------------


def _range_flag(
    code: str, field_name: str, value: float, bounds: tuple[float, float], unit: str
) -> dict[str, Any] | None:
    lo, hi = bounds
    if lo <= value <= hi:
        return None
    return {
        "code": code,
        "kind": QC_KIND_PLAUSIBILITY,
        "field": field_name,
        "value": value,
        "min": lo,
        "max": hi,
        "message": (
            f"{field_name} = {value:g} {unit} is outside the plausible range {lo:g}-{hi:g} {unit}."
        ),
    }


def check_plausibility(
    *,
    icv_mm3: float | None = None,
    hippocampus_total_mm3: float | None = None,
    cortical_thickness_mm: float | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """QC flags for values outside physiological plausibility. Never raises:
    an implausible value is recorded, and the engine declines to stage it.
    """
    settings = settings or get_settings()
    candidates = [
        (icv_mm3, "icv_out_of_range", "icv_mm3", settings.plausibility_icv_mm3, "mm3"),
        (
            hippocampus_total_mm3,
            "hippocampus_out_of_range",
            "hippocampus_total_mm3",
            settings.plausibility_hippocampus_total_mm3,
            "mm3",
        ),
        (
            cortical_thickness_mm,
            "thickness_out_of_range",
            "cortical_thickness_mm",
            settings.plausibility_thickness_mm,
            "mm",
        ),
    ]
    flags = []
    for value, code, field_name, bounds, unit in candidates:
        if value is None:
            continue
        flag = _range_flag(code, field_name, value, bounds, unit)
        if flag is not None:
            flags.append(flag)
    return flags


def _info(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "kind": QC_KIND_INFO, "message": message}


# --------------------------------------------------------------------------
# Assembling a DerivedBiomarkers
# --------------------------------------------------------------------------


def _aseg_raw(aseg: AsegStats) -> dict[str, Any]:
    return {
        "icv_mm3": aseg.icv_mm3,
        "icv_source": aseg.icv_source,
        "brain_seg_vol_mm3": aseg.brain_seg_vol_mm3,
        "left_hippocampus_mm3": aseg.left_hippocampus_mm3,
        "right_hippocampus_mm3": aseg.right_hippocampus_mm3,
        "left_lateral_ventricle_mm3": aseg.left_lateral_ventricle_mm3,
        "right_lateral_ventricle_mm3": aseg.right_lateral_ventricle_mm3,
        "left_inf_lat_vent_mm3": aseg.left_inf_lat_vent_mm3,
        "right_inf_lat_vent_mm3": aseg.right_inf_lat_vent_mm3,
        "third_ventricle_mm3": aseg.third_ventricle_mm3,
        "fourth_ventricle_mm3": aseg.fourth_ventricle_mm3,
        "left_cerebral_cortex_mm3": aseg.left_cerebral_cortex_mm3,
        "right_cerebral_cortex_mm3": aseg.right_cerebral_cortex_mm3,
        "cortex_source": aseg.cortex_source,
    }


def derive_from_volumes(
    *,
    icv_mm3: float,
    brain_seg_vol_mm3: float,
    left_hippocampus_mm3: float,
    right_hippocampus_mm3: float,
    left_lateral_ventricle_mm3: float,
    right_lateral_ventricle_mm3: float,
    left_inf_lat_vent_mm3: float,
    right_inf_lat_vent_mm3: float,
    cortical_thickness_mm: float | None,
    raw_volumes: Mapping[str, Any],
    extra_flags: Sequence[dict[str, Any]] = (),
    left_cerebral_cortex_mm3: float | None = None,
    right_cerebral_cortex_mm3: float | None = None,
    settings: Settings | None = None,
) -> DerivedBiomarkers:
    """The single place raw volumes become engine inputs. Both the stats
    parser path and the raw-volumes JSON path end here.
    """
    hippocampus_total = left_hippocampus_mm3 + right_hippocampus_mm3
    raw: dict[str, Any] = dict(raw_volumes)
    raw["hippocampus_total_mm3"] = hippocampus_total
    raw["cortical_gm_volume_norm"] = (
        compute_cortical_gm_volume_norm(
            left_cerebral_cortex_mm3, right_cerebral_cortex_mm3, icv_mm3
        )
        if left_cerebral_cortex_mm3 is not None and right_cerebral_cortex_mm3 is not None
        else None
    )

    flags = list(extra_flags)
    flags += check_plausibility(
        icv_mm3=icv_mm3,
        hippocampus_total_mm3=hippocampus_total,
        cortical_thickness_mm=cortical_thickness_mm,
        settings=settings,
    )

    return DerivedBiomarkers(
        values={
            HV: compute_hippocampal_volume_norm(
                left_hippocampus_mm3, right_hippocampus_mm3, icv_mm3
            ),
            VBR: compute_vbr(
                left_lateral_ventricle_mm3,
                right_lateral_ventricle_mm3,
                left_inf_lat_vent_mm3,
                right_inf_lat_vent_mm3,
                brain_seg_vol_mm3,
            ),
            CT: cortical_thickness_mm,
        },
        units=dict(DERIVED_UNITS),
        raw_volumes=raw,
        qc_flags=flags,
    )


def derive_from_stats(stats: ParsedStats, *, settings: Settings | None = None) -> DerivedBiomarkers:
    """Engine inputs from parsed FreeSurfer / FastSurfer stats files."""
    settings = settings or get_settings()
    aseg = stats.aseg
    raw = _aseg_raw(aseg)
    flags: list[dict[str, Any]] = []

    if aseg.icv_source != "eTIV":
        flags.append(
            _info(
                "icv_from_legacy_measure",
                f"Intracranial volume read from the '{aseg.icv_source}' measure, not eTIV.",
            )
        )
    for name, value in (
        ("3rd-Ventricle", aseg.third_ventricle_mm3),
        ("4th-Ventricle", aseg.fourth_ventricle_mm3),
        ("cerebral cortex", aseg.left_cerebral_cortex_mm3),
    ):
        if value is None:
            flags.append(_info("structure_absent", f"The aseg stats have no {name} volume."))

    thickness: float | None = None
    if stats.lh_aparc is not None and stats.rh_aparc is not None:
        regions = settings.cortical_thickness_regions
        thickness = compute_cortical_thickness(
            stats.lh_aparc,
            stats.rh_aparc,
            regions,
            weighting=settings.cortical_thickness_weighting,
        )
        raw["thickness_regions"] = list(regions)
        raw["thickness_weighting"] = settings.cortical_thickness_weighting
        raw["thickness_mm"] = {
            s.hemisphere: {name: r.thick_avg_mm for name, r in s.regions.items()}
            for s in (stats.lh_aparc, stats.rh_aparc)
        }
        raw["surface_area_mm2"] = {
            s.hemisphere: {name: r.surf_area_mm2 for name, r in s.regions.items()}
            for s in (stats.lh_aparc, stats.rh_aparc)
        }
    else:
        flags.append(
            _info(
                "cortical_thickness_unavailable",
                "No aparc stats were supplied, so cortical thickness was not measured.",
            )
        )

    return derive_from_volumes(
        icv_mm3=aseg.icv_mm3,
        brain_seg_vol_mm3=aseg.brain_seg_vol_mm3,
        left_hippocampus_mm3=aseg.left_hippocampus_mm3,
        right_hippocampus_mm3=aseg.right_hippocampus_mm3,
        left_lateral_ventricle_mm3=aseg.left_lateral_ventricle_mm3,
        right_lateral_ventricle_mm3=aseg.right_lateral_ventricle_mm3,
        left_inf_lat_vent_mm3=aseg.left_inf_lat_vent_mm3,
        right_inf_lat_vent_mm3=aseg.right_inf_lat_vent_mm3,
        cortical_thickness_mm=thickness,
        left_cerebral_cortex_mm3=aseg.left_cerebral_cortex_mm3,
        right_cerebral_cortex_mm3=aseg.right_cerebral_cortex_mm3,
        raw_volumes=raw,
        extra_flags=flags,
        settings=settings,
    )


def derive_from_values(
    *,
    hippocampal_volume_norm: float | None,
    ventricle_brain_ratio: float | None,
    cortical_thickness_mm: float | None,
    raw_volumes: Mapping[str, Any] | None = None,
    icv_mm3: float | None = None,
    hippocampus_total_mm3: float | None = None,
    settings: Settings | None = None,
) -> DerivedBiomarkers:
    """Engine inputs supplied already derived (a JSON body or a cohort CSV
    row), in the canonical units of DERIVED_UNITS.

    If the raw ICV and total hippocampal volume are supplied alongside, the
    normalised value is recomputed from them and a disagreement is flagged:
    it catches a `hipp_norm` column that is a fraction or a percentage
    rather than per-mille.
    """
    raw: dict[str, Any] = dict(raw_volumes or {})
    flags = check_plausibility(
        icv_mm3=icv_mm3,
        hippocampus_total_mm3=hippocampus_total_mm3,
        cortical_thickness_mm=cortical_thickness_mm,
        settings=settings,
    )

    if (
        hippocampal_volume_norm is not None
        and icv_mm3 is not None
        and hippocampus_total_mm3 is not None
        and icv_mm3 > 0
    ):
        recomputed = hippocampus_total_mm3 / icv_mm3 * 1000.0
        if abs(recomputed - hippocampal_volume_norm) > _CONSISTENCY_REL_TOL * abs(recomputed):
            flags.append(
                {
                    "code": "hippocampal_norm_inconsistent",
                    "kind": QC_KIND_PLAUSIBILITY,
                    "field": "hippocampal_volume",
                    "value": hippocampal_volume_norm,
                    "expected": recomputed,
                    "message": (
                        f"Supplied hippocampal volume {hippocampal_volume_norm:g} does not equal "
                        f"hippocampus / ICV x 1000 = {recomputed:g}. Check that it is in per-mille "
                        "of ICV."
                    ),
                }
            )

    return DerivedBiomarkers(
        values={
            HV: hippocampal_volume_norm,
            VBR: ventricle_brain_ratio,
            CT: cortical_thickness_mm,
        },
        units=dict(DERIVED_UNITS),
        raw_volumes=raw,
        qc_flags=flags,
    )
