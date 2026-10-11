"""Fuzzy inference engine for CN / MCI / AD staging.

What this module computes, in order:

1. **Preconditions.** Before any rule runs, the engine checks that it is
   entitled to an opinion: the calibration is complete, it was fitted to
   the same kind of data it is being asked about, all three biomarkers are
   present in the units the calibration expects, and none of them carries a
   plausibility QC flag. If a check fails the result is *indeterminate*:
   no stage, no severity, flagged for review, with the reason recorded.
2. **Fuzzification and rules.** Each biomarker is fuzzified against the
   calibration's trapezoidal sets. Fuzzy AND is min and fuzzy OR is max
   (Zadeh operators). Each rule yields a firing strength in [0, 1], and a
   stage's activation is the max strength over the rules concluding it.
   If no rule fires at all the result is indeterminate (C4, closed: this
   path used to fall through to CN).
3. **Stage, confidence, uncertainty.** The three activations are
   normalised to sum to 1. The stage is the largest share, `confidence` is
   that share and `uncertainty` is one minus its lead over the runner-up.
   These are unchanged from earlier versions. They are shares of rule
   activation, not probabilities (C6 and the confidence-ring concern in
   C10 are still open).
4. **Severity** (C10, mechanics implemented). `defuzzify_centroid` does
   Mamdani centroid defuzzification: each stage's output set is clipped at
   that stage's activation, the clipped sets are max-aggregated, and the
   centroid of the aggregate over the 0-1 severity axis is reported as
   `severity`. The output sets come from the calibration file. The ones
   shipped in `phantom.v0.json` are placeholders marked provisional, so
   `severity` has the right mechanics and no clinical meaning yet. Choosing
   real output sets is the part of C10 that remains open.

The rule base below is illustrative: informed by the literature reviewed
for the project proposal, not yet reviewed by Dr. Deshmukh. It is kept in
one readable place so it can be revised without touching the inference
machinery. Rule content and weights are out of scope for the plumbing
work; C5 (R6's 0.75 weight) is still open.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.models.enums import BiomarkerKey, Stage
from app.services.biomarkers import FuzzyLabel, trapmf
from app.services.calibration import Calibration, DataDomain, get_calibration

FuzzySnapshot = Mapping[BiomarkerKey, Mapping[FuzzyLabel, float]]

# Recorded on every scan so a stored result can be traced to the inference
# code that produced it. Bump when the engine's output can change for the
# same inputs and calibration.
ENGINE_VERSION = "2.0.0"


def fAND(*degrees: float) -> float:
    return min(degrees) if degrees else 0.0


def fOR(*degrees: float) -> float:
    return max(degrees) if degrees else 0.0


def _deg(fuzzy: FuzzySnapshot, key: BiomarkerKey, label: FuzzyLabel) -> float:
    return fuzzy[key].get(label, 0.0)


HV = BiomarkerKey.HIPPOCAMPAL_VOLUME
VBR = BiomarkerKey.VENTRICLE_BRAIN_RATIO
CT = BiomarkerKey.CORTICAL_THICKNESS
LOW, BORDER, NORMAL, HIGH = (
    FuzzyLabel.LOW,
    FuzzyLabel.BORDERLINE,
    FuzzyLabel.NORMAL,
    FuzzyLabel.HIGH,
)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    antecedent_text: str
    consequent: Stage
    evaluate: Callable[[FuzzySnapshot], float]


@dataclass(frozen=True, slots=True)
class FiredRule:
    id: str
    antecedent: str
    consequent: Stage
    firing_strength: float


RULES: list[Rule] = [
    Rule(
        "R1",
        "IF Hippocampus, VBR and Cortex are all normal",
        Stage.CN,
        lambda f: fAND(_deg(f, HV, NORMAL), _deg(f, VBR, NORMAL), _deg(f, CT, NORMAL)),
    ),
    Rule(
        "R2",
        "IF Hippocampus and Cortex are normal AND VBR is at most borderline",
        Stage.CN,
        lambda f: fAND(
            _deg(f, HV, NORMAL),
            _deg(f, CT, NORMAL),
            fOR(_deg(f, VBR, NORMAL), _deg(f, VBR, BORDER)),
        ),
    ),
    Rule(
        "R3",
        "IF Hippocampus is atrophied AND VBR is enlarged",
        Stage.AD,
        lambda f: fAND(_deg(f, HV, LOW), _deg(f, VBR, HIGH)),
    ),
    Rule(
        "R4",
        "IF Hippocampus is atrophied AND Cortex is thinned",
        Stage.AD,
        lambda f: fAND(_deg(f, HV, LOW), _deg(f, CT, LOW)),
    ),
    Rule(
        "R5",
        "IF VBR is enlarged AND Cortex is thinned",
        Stage.AD,
        lambda f: fAND(_deg(f, VBR, HIGH), _deg(f, CT, LOW)),
    ),
    Rule(
        "R6",
        "IF any single biomarker is severely abnormal, independent of the others",
        Stage.AD,
        # TODO(clinical: C5) -- 0.75 is unsourced. Every other rule is
        # weighted 1.0, so this encodes a stance on how much a single
        # severe biomarker is worth relative to two concurring ones.
        lambda f: 0.75 * fOR(_deg(f, HV, LOW), _deg(f, VBR, HIGH), _deg(f, CT, LOW)),
    ),
    Rule(
        "R7",
        "IF Hippocampus is borderline AND VBR is not enlarged",
        Stage.MCI,
        lambda f: fAND(_deg(f, HV, BORDER), fOR(_deg(f, VBR, NORMAL), _deg(f, VBR, BORDER))),
    ),
    Rule(
        "R8",
        "IF Cortex is borderline AND Hippocampus is not atrophied",
        Stage.MCI,
        lambda f: fAND(_deg(f, CT, BORDER), fOR(_deg(f, HV, NORMAL), _deg(f, HV, BORDER))),
    ),
    Rule(
        "R9",
        "IF VBR is borderline AND at least one other biomarker is normal",
        Stage.MCI,
        lambda f: fAND(_deg(f, VBR, BORDER), fOR(_deg(f, HV, NORMAL), _deg(f, CT, NORMAL))),
    ),
    Rule(
        "R10",
        "IF two or more biomarkers are jointly borderline",
        Stage.MCI,
        lambda f: fOR(
            fAND(_deg(f, HV, BORDER), _deg(f, CT, BORDER)),
            fAND(_deg(f, HV, BORDER), _deg(f, VBR, BORDER)),
            fAND(_deg(f, CT, BORDER), _deg(f, VBR, BORDER)),
        ),
    ),
]


# Reason codes for an indeterminate result. A result's `indeterminate_reason`
# is one or more of these joined by "; ", each optionally followed by
# ":<detail>" naming the biomarkers or QC flags involved.
REASON_CALIBRATION_INCOMPLETE = "calibration_incomplete"
REASON_CALIBRATION_NOT_REAL = "calibration_not_validated_for_real_data"
REASON_CALIBRATION_DOMAIN_MISMATCH = "calibration_domain_mismatch"
REASON_MISSING_BIOMARKER = "missing_biomarker"
REASON_UNIT_MISMATCH = "unit_mismatch"
REASON_QC_FLAG = "qc_flag"
REASON_NO_RULE_FIRED = "no_rule_fired"

# QC flags of this kind make a result indeterminate; "info" flags do not.
QC_KIND_PLAUSIBILITY = "plausibility"

# The severity axis. Fixed by the engine's contract (severity is reported
# on 0-1), not a calibration parameter.
SEVERITY_AXIS: tuple[float, float] = (0.0, 1.0)
_CENTROID_SAMPLES = 1001


@dataclass(frozen=True, slots=True)
class FISOutput:
    stage: Stage | None  # None when the result is indeterminate
    confidence: float  # 0-100
    uncertainty: float  # 0-100
    membership: dict[Stage, float]  # normalized, sums to 1 (all 0 when indeterminate)
    fired_rules: list[FiredRule]
    needs_review: bool
    determinate: bool = True
    indeterminate_reason: str | None = None
    severity: float | None = None  # 0-1 centroid, see defuzzify_centroid
    calibration_version: str = ""


def calibration_mismatch_reasons(
    calibration: Calibration, data_domain: DataDomain | None
) -> list[str]:
    """Why this calibration may not be applied to data of this domain, or
    an empty list if it may. `data_domain=None` means a what-if evaluation
    in the calibration's own terms, with no real measurement behind it.
    """
    if not calibration.complete:
        return [REASON_CALIBRATION_INCOMPLETE]
    if data_domain is None or data_domain == calibration.domain:
        return []
    if data_domain == DataDomain.REAL:
        return [REASON_CALIBRATION_NOT_REAL]
    return [REASON_CALIBRATION_DOMAIN_MISMATCH]


def defuzzify_centroid(
    activation: Mapping[Stage, float],
    output_sets: Mapping[Stage, tuple[float, float, float, float]],
    *,
    samples: int = _CENTROID_SAMPLES,
) -> float | None:
    """Mamdani centroid defuzzification on the 0-1 severity axis.

    Each stage's output set (a trapezoid from the calibration file) is
    clipped at that stage's activation, the clipped sets are aggregated
    with max, and the centroid of the aggregate is returned. Returns None
    when the aggregate has no area, which happens only if nothing fired.

    Note the centroid of a clipped set is pulled toward the middle of the
    axis: with triangular end sets a pure-CN case scores above 0 and a
    pure-AD case below 1. That is a property of the method, not a bug.
    """
    lo, hi = SEVERITY_AXIS
    xs = np.linspace(lo, hi, samples)
    aggregate = np.zeros_like(xs)
    for stage, params in output_sets.items():
        level = min(max(float(activation.get(stage, 0.0)), 0.0), 1.0)
        if level <= 0.0:
            continue
        shape = np.array([trapmf(float(x), *params) for x in xs])
        aggregate = np.maximum(aggregate, np.minimum(shape, level))

    area = float(np.trapezoid(aggregate, xs))
    if area <= 1e-12:
        return None
    centroid = float(np.trapezoid(aggregate * xs, xs)) / area
    return round(min(max(centroid, lo), hi), 4)


def _indeterminate(reasons: Sequence[str], calibration: Calibration) -> FISOutput:
    """The engine declining to answer. Carries no stage and no engine
    numbers that could be mistaken for one: zero membership, zero
    confidence, full uncertainty, always flagged for review.
    """
    return FISOutput(
        stage=None,
        confidence=0.0,
        uncertainty=100.0,
        membership=dict.fromkeys(Stage, 0.0),
        fired_rules=[],
        needs_review=True,
        determinate=False,
        indeterminate_reason="; ".join(reasons)[:500],
        severity=None,
        calibration_version=calibration.version,
    )


def _is_usable(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def run_fis(
    raw_values: Mapping[BiomarkerKey, float | None],
    *,
    review_threshold: float = 45.0,
    calibration: Calibration | None = None,
    data_domain: DataDomain | None = None,
    units: Mapping[BiomarkerKey, str] | None = None,
    qc_flags: Sequence[Mapping[str, Any]] = (),
) -> FISOutput:
    """Stage one case.

    `data_domain` is where the values came from. Leave it None only for a
    what-if evaluation (the `/fis/simulate` endpoint). `units` are the
    units the values are in; when given, they must equal the calibration's.
    `qc_flags` are the flags raised while deriving the values.
    """
    calibration = calibration or get_calibration()

    reasons = calibration_mismatch_reasons(calibration, data_domain)
    if REASON_CALIBRATION_INCOMPLETE in reasons:
        return _indeterminate(reasons, calibration)

    missing = [key.value for key in BiomarkerKey if not _is_usable(raw_values.get(key))]
    if missing:
        reasons.append(f"{REASON_MISSING_BIOMARKER}:{','.join(missing)}")

    if units is not None:
        mismatched = [
            key.value
            for key in BiomarkerKey
            if key in units and units[key] != calibration.units[key]
        ]
        if mismatched:
            reasons.append(f"{REASON_UNIT_MISMATCH}:{','.join(mismatched)}")

    flagged = sorted(
        {str(f.get("code", "unknown")) for f in qc_flags if f.get("kind") == QC_KIND_PLAUSIBILITY}
    )
    if flagged:
        reasons.append(f"{REASON_QC_FLAG}:{','.join(flagged)}")

    if reasons:
        return _indeterminate(reasons, calibration)

    fuzzy: dict[BiomarkerKey, dict[FuzzyLabel, float]] = {}
    for key in BiomarkerKey:
        value = raw_values[key]
        assert value is not None  # guaranteed by the missing-biomarker check above
        fuzzy[key] = calibration.biomarkers[key].fuzzify(value)

    fired: list[FiredRule] = []
    activation: dict[Stage, float] = dict.fromkeys(Stage, 0.0)

    for rule in RULES:
        strength = round(rule.evaluate(fuzzy), 4)
        if strength > 0.0:
            fired.append(FiredRule(rule.id, rule.antecedent_text, rule.consequent, strength))
        activation[rule.consequent] = max(activation[rule.consequent], strength)

    total = sum(activation.values())
    if total <= 1e-9:
        # No rule fired: the values lie outside every fuzzy set. The engine
        # has no evidence for any stage and says so (C4, closed). This path
        # used to report three equal shares, which the ranking below then
        # resolved to CN: the reassuring answer, given exactly when the
        # system knew nothing.
        return _indeterminate([REASON_NO_RULE_FIRED], calibration)

    membership = {stage: round(v / total, 4) for stage, v in activation.items()}

    ranked = sorted(membership.items(), key=lambda kv: kv[1], reverse=True)
    top_stage, top_degree = ranked[0]
    _, second_degree = ranked[1]

    confidence = round(top_degree * 100, 1)
    separation = top_degree - second_degree
    uncertainty = round((1 - separation) * 100, 1)

    fired.sort(key=lambda r: r.firing_strength, reverse=True)

    severity = (
        defuzzify_centroid(activation, calibration.output_sets)
        if calibration.output_sets is not None
        else None
    )

    return FISOutput(
        stage=top_stage,
        confidence=confidence,
        uncertainty=uncertainty,
        membership=membership,
        fired_rules=fired,
        needs_review=uncertainty >= review_threshold,
        determinate=True,
        indeterminate_reason=None,
        severity=severity,
        calibration_version=calibration.version,
    )
