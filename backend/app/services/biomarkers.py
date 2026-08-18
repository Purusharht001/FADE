"""Biomarker definitions and fuzzification.

Each biomarker is described by three overlapping fuzzy sets (trapezoidal
membership functions) fitted to illustrative clinical breakpoints. This is
the Phase 3/4 reference implementation referred to in the project's
PHASES.md — the breakpoints below are a starting point designed to be
replaced during the clinician-guided rule design step, not a validated
diagnostic threshold.

Hippocampal volume's breakpoints are in literature-plausible clinical mL.
Ventricle-to-brain ratio and cortical thickness are instead calibrated to
what `volumetry.py`'s pipeline actually measures on the synthetic MRI
phantom (`synthetic_mri.py`) end-to-end: that phantom's voxel resolution
(2mm isotropic) is too coarse to resolve a literal ~2.5mm cortical ribbon
as a multi-voxel shell, and its ventricle-to-total-brain-mask proportions
are geometrically smaller than a real head's, so literal clinical
percentages/mm would put every synthetic case in the same fuzzy bucket
regardless of severity. Recalibrating to the phantom's own achievable
range keeps the demo's severity dial meaningful end-to-end; real
breakpoints would come from Phase 1-4 clinical/literature calibration
against real scans, not from this phantom.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.models.enums import BiomarkerKey


class FuzzyLabel(StrEnum):
    LOW = "low"  # atrophied / thinned, depending on the biomarker
    BORDERLINE = "borderline"
    NORMAL = "normal"
    HIGH = "high"  # enlarged — only ventricle-to-brain ratio uses this pole


def trapmf(x: float, a: float, b: float, c: float, d: float) -> float:
    """Trapezoidal membership function: 0 below a, rises to 1 over [a,b],
    plateaus at 1 over [b,c], falls to 0 over [c,d], 0 above d.

    The plateau check runs before the outside-support check so a degenerate
    shoulder (a == b, as several of this project's "fully normal from here"
    sets use) still reads as full membership exactly at x == a, rather than
    the outside-support branch catching `x <= a` first and reporting 0 for
    the single most-normal value the set is meant to include.
    """
    if b <= x <= c:
        return 1.0
    if x <= a or x >= d:
        return 0.0
    if a < x < b:
        return (x - a) / (b - a)
    return (d - x) / (d - c)


@dataclass(frozen=True, slots=True)
class BiomarkerDef:
    key: BiomarkerKey
    label: str
    short_label: str
    unit: str
    normal_range: tuple[float, float]
    lower_is_worse: bool
    description: str
    sets: dict[FuzzyLabel, tuple[float, float, float, float]]
    # Sensible x-axis bounds for plotting this biomarker's membership
    # curves — deliberately explicit rather than inferred from the sets'
    # own breakpoints, since a set's "fully normal from here on" shoulder
    # is intentionally a wide/arbitrary plateau (e.g. cortical thickness's
    # NORMAL set runs out to 10mm), which would make a poor chart axis.
    display_range: tuple[float, float]

    def fuzzify(self, value: float) -> dict[FuzzyLabel, float]:
        return {label: round(trapmf(value, *params), 4) for label, params in self.sets.items()}

    def sample_curve(self, steps: int = 60) -> dict[FuzzyLabel, list[tuple[float, float]]]:
        """Samples each fuzzy set's membership curve across `display_range`
        — for the API to hand the frontend literal (x, degree) points to
        plot, so no fuzzy-logic computation (trapmf or otherwise) needs to
        happen client-side. Rendering pre-computed points is not the same
        as computing clinical inference.
        """
        lo, hi = self.display_range
        xs = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
        return {
            label: [(round(x, 4), round(trapmf(x, *params), 4)) for x in xs]
            for label, params in self.sets.items()
        }

    def abnormality(self, value: float) -> float:
        """Scalar 0 (normal) .. 1 (fully abnormal) severity, derived from the
        fuzzy sets rather than a separate linear formula, so the number shown
        to a clinician is consistent with what actually drove the rule base.
        """
        degrees = self.fuzzify(value)
        worst_label = FuzzyLabel.HIGH if FuzzyLabel.HIGH in self.sets else FuzzyLabel.LOW
        normal = degrees.get(FuzzyLabel.NORMAL, 0.0)
        worst = degrees.get(worst_label, 0.0)
        borderline = degrees.get(FuzzyLabel.BORDERLINE, 0.0)
        # Weighted blend: fully in the worst set -> 1.0, fully normal -> 0.0,
        # fully borderline -> 0.5, with linear blending in between.
        return round(min(1.0, max(0.0, worst + 0.5 * borderline + 0.0 * normal)), 4)


BIOMARKER_DEFS: dict[BiomarkerKey, BiomarkerDef] = {
    BiomarkerKey.HIPPOCAMPAL_VOLUME: BiomarkerDef(
        key=BiomarkerKey.HIPPOCAMPAL_VOLUME,
        label="Hippocampal Volume",
        short_label="Hippocampus",
        unit="mL",
        normal_range=(3.0, 4.5),
        lower_is_worse=True,
        description=(
            "Bilateral hippocampal volume, normalized for intracranial volume. Atrophy here is "
            "one of the earliest structural markers of AD-related neurodegeneration."
        ),
        sets={
            FuzzyLabel.LOW: (0.0, 0.0, 2.0, 2.6),
            FuzzyLabel.BORDERLINE: (2.2, 2.6, 3.0, 3.4),
            FuzzyLabel.NORMAL: (3.0, 3.6, 6.0, 6.0),
        },
        display_range=(0.0, 6.5),
    ),
    BiomarkerKey.VENTRICLE_BRAIN_RATIO: BiomarkerDef(
        key=BiomarkerKey.VENTRICLE_BRAIN_RATIO,
        label="Ventricle-to-Brain Ratio",
        short_label="VBR",
        unit="%",
        normal_range=(0.0, 0.5),
        lower_is_worse=False,
        description=(
            "Lateral ventricle volume as a percentage of total brain volume. Enlargement is a "
            "proxy for global tissue loss (ex-vacuo dilation). Percentages are calibrated to this "
            "project's synthetic phantom, not literal clinical VBR — see module docstring."
        ),
        sets={
            FuzzyLabel.NORMAL: (0.0, 0.0, 0.3, 0.5),
            FuzzyLabel.BORDERLINE: (0.35, 0.5, 0.75, 0.95),
            FuzzyLabel.HIGH: (0.75, 0.95, 3.0, 3.0),
        },
        display_range=(0.0, 1.6),
    ),
    BiomarkerKey.CORTICAL_THICKNESS: BiomarkerDef(
        key=BiomarkerKey.CORTICAL_THICKNESS,
        label="Mean Cortical Thickness",
        short_label="Cortex",
        unit="mm",
        normal_range=(5.6, 6.0),
        lower_is_worse=True,
        description=(
            "Average thickness across AD-signature cortical regions (entorhinal, temporal, "
            "parietal). Thinning tracks cumulative neuronal loss. Millimeters are calibrated to "
            "this project's coarse-resolution synthetic phantom, not literal clinical cortical "
            "thickness — see module docstring."
        ),
        sets={
            FuzzyLabel.LOW: (0.0, 0.0, 4.9, 5.2),
            FuzzyLabel.BORDERLINE: (4.9, 5.2, 5.6, 5.85),
            FuzzyLabel.NORMAL: (5.6, 5.85, 10.0, 10.0),
        },
        display_range=(4.5, 7.0),
    ),
}


def fuzzify_all(
    values: Mapping[BiomarkerKey, float],
) -> dict[BiomarkerKey, dict[FuzzyLabel, float]]:
    return {key: BIOMARKER_DEFS[key].fuzzify(value) for key, value in values.items()}
