"""Biomarker definitions and fuzzification.

Each biomarker is described by three overlapping fuzzy sets (trapezoidal
membership functions). This module holds the mechanics (`trapmf`,
`BiomarkerDef`) and the structural half of each definition
(`BIOMARKER_STRUCTURE`). It holds no numbers: every breakpoint,
`normal_range`, `display_range`, unit and description is read from a
versioned calibration file under `app/calibration/` by
`app/services/calibration.py`.

`phantom.v0.json` carries the values that used to be literals here. They
are fitted to what `volumetry.py` measures on the synthetic phantom
(`synthetic_mri.py`), whose 2 mm voxels cannot resolve a real ~2.5 mm
cortical ribbon and whose ventricle proportions are smaller than a real
head's. They are not clinical thresholds, which is why the engine refuses
to stage real data with that file (see `fis_engine.py`).

Open clinical assumptions that still apply to those numbers, tracked in
docs/ARCHITECTURE.md: C3 (phantom-scale VBR and thickness breakpoints),
C7 (the 0.5 borderline weight in `abnormality`), C8 (`normal_range`
disagrees with the NORMAL set).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.models.enums import BiomarkerKey

# Unit strings. A measurement carries the unit of the instrument that
# produced it, a calibration file declares the unit its breakpoints are in,
# and the engine refuses to combine the two unless they are equal.
UNIT_ML = "mL"  # in-house phantom volumetry: raw hippocampal volume
UNIT_PERCENT = "%"  # in-house phantom volumetry: ventricle-to-brain ratio
UNIT_MM = "mm"
UNIT_PERMILLE_ICV = "‰ICV"  # real data: volume / ICV x 1000
UNIT_RATIO = "ratio"  # real data: unitless ventricle-to-brain ratio


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
        # TODO(clinical: C7) -- the 0.5 asserts a fully borderline
        # biomarker is exactly half as concerning as a fully abnormal one.
        # Unsourced, and shown to clinicians as a per-biomarker severity.
        return round(min(1.0, max(0.0, worst + 0.5 * borderline + 0.0 * normal)), 4)


@dataclass(frozen=True, slots=True)
class BiomarkerStructure:
    """The part of a biomarker definition that is code, not calibration:
    its names, which direction is worse, and which fuzzy sets the rule
    base in fis_engine.py refers to. Every number, the unit and the
    clinician-facing description live in the calibration file instead.
    """

    key: BiomarkerKey
    label: str
    short_label: str
    lower_is_worse: bool
    set_labels: tuple[FuzzyLabel, ...]


BIOMARKER_STRUCTURE: dict[BiomarkerKey, BiomarkerStructure] = {
    BiomarkerKey.HIPPOCAMPAL_VOLUME: BiomarkerStructure(
        key=BiomarkerKey.HIPPOCAMPAL_VOLUME,
        label="Hippocampal Volume",
        short_label="Hippocampus",
        lower_is_worse=True,
        set_labels=(FuzzyLabel.LOW, FuzzyLabel.BORDERLINE, FuzzyLabel.NORMAL),
    ),
    BiomarkerKey.VENTRICLE_BRAIN_RATIO: BiomarkerStructure(
        key=BiomarkerKey.VENTRICLE_BRAIN_RATIO,
        label="Ventricle-to-Brain Ratio",
        short_label="VBR",
        lower_is_worse=False,
        set_labels=(FuzzyLabel.NORMAL, FuzzyLabel.BORDERLINE, FuzzyLabel.HIGH),
    ),
    BiomarkerKey.CORTICAL_THICKNESS: BiomarkerStructure(
        key=BiomarkerKey.CORTICAL_THICKNESS,
        label="Mean Cortical Thickness",
        short_label="Cortex",
        lower_is_worse=True,
        set_labels=(FuzzyLabel.LOW, FuzzyLabel.BORDERLINE, FuzzyLabel.NORMAL),
    ),
}


def __getattr__(name: str) -> dict[BiomarkerKey, BiomarkerDef]:
    """`BIOMARKER_DEFS` used to be a literal in this module. It is now the
    biomarker definitions of the active calibration file, resolved on
    access so existing `from app.services.biomarkers import BIOMARKER_DEFS`
    imports keep working. New code should call
    `app.services.calibration.get_calibration()` and say which calibration
    it means.
    """
    if name == "BIOMARKER_DEFS":
        from app.services.calibration import get_calibration

        return get_calibration().biomarkers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def fuzzify_all(
    values: Mapping[BiomarkerKey, float],
) -> dict[BiomarkerKey, dict[FuzzyLabel, float]]:
    from app.services.calibration import get_calibration

    defs = get_calibration().biomarkers
    return {key: defs[key].fuzzify(value) for key, value in values.items()}
