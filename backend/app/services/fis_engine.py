"""Mamdani-style fuzzy inference engine for CN / MCI / AD staging.

This is the Phase 4/5 reference implementation: fuzzy AND is min, fuzzy OR
is max (standard Zadeh operators), each rule's antecedent produces a firing
strength in [0, 1], and per-stage output activation is the max firing
strength across every rule concluding that stage (Mamdani max aggregation).
The three stage activations are then normalized into a membership
distribution so they read as "confidence shares" — the winning stage's
share is the reported confidence, and the gap to the runner-up drives the
uncertainty score that the triage dashboard sorts on.

The rule base below is illustrative — informed by the literature reviewed
for the project proposal, not yet reviewed by Dr. Deshmukh. It is deliberately
kept in one readable place so it is easy to revise during Phase 4 (clinician-
guided rule design) and Phase 9 (feedback-loop iteration) without touching
the inference machinery itself.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.models.enums import BiomarkerKey, Stage
from app.services.biomarkers import BIOMARKER_DEFS, FuzzyLabel

FuzzySnapshot = Mapping[BiomarkerKey, Mapping[FuzzyLabel, float]]


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


@dataclass(frozen=True, slots=True)
class FISOutput:
    stage: Stage
    confidence: float  # 0-100
    uncertainty: float  # 0-100
    membership: dict[Stage, float]  # normalized, sums to 1
    fired_rules: list[FiredRule]
    needs_review: bool


def run_fis(
    raw_values: Mapping[BiomarkerKey, float], *, review_threshold: float = 45.0
) -> FISOutput:
    fuzzy: dict[BiomarkerKey, dict[FuzzyLabel, float]] = {
        key: BIOMARKER_DEFS[key].fuzzify(value) for key, value in raw_values.items()
    }

    fired: list[FiredRule] = []
    activation: dict[Stage, float] = dict.fromkeys(Stage, 0.0)

    for rule in RULES:
        strength = round(rule.evaluate(fuzzy), 4)
        if strength > 0.0:
            fired.append(FiredRule(rule.id, rule.antecedent_text, rule.consequent, strength))
        activation[rule.consequent] = max(activation[rule.consequent], strength)

    total = sum(activation.values())
    if total <= 1e-9:
        # No rule fired at all (values far outside every fuzzy set) — treat as
        # maximally uncertain rather than silently defaulting to one stage.
        membership = dict.fromkeys(Stage, 1 / 3)
    else:
        membership = {stage: round(v / total, 4) for stage, v in activation.items()}

    ranked = sorted(membership.items(), key=lambda kv: kv[1], reverse=True)
    top_stage, top_degree = ranked[0]
    _, second_degree = ranked[1]

    confidence = round(top_degree * 100, 1)
    separation = top_degree - second_degree
    uncertainty = round((1 - separation) * 100, 1)

    fired.sort(key=lambda r: r.firing_strength, reverse=True)

    return FISOutput(
        stage=top_stage,
        confidence=confidence,
        uncertainty=uncertainty,
        membership=membership,
        fired_rules=fired,
        needs_review=uncertainty >= review_threshold,
    )
