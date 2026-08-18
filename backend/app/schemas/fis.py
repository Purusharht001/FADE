from pydantic import Field

from app.models.enums import BiomarkerKey, Stage
from app.schemas.base import CamelModel


class BiomarkerReadingRead(CamelModel):
    key: BiomarkerKey
    value: float
    unit: str
    abnormality: float = Field(ge=0, le=1)


class FiredRuleRead(CamelModel):
    id: str
    antecedent: str
    consequent: Stage
    firing_strength: float = Field(ge=0, le=1)


class FISResultRead(CamelModel):
    stage: Stage
    confidence: float = Field(ge=0, le=100)
    uncertainty: float = Field(ge=0, le=100)
    needs_review: bool
    reviewed: bool = False
    membership: dict[Stage, float]
    fired_rules: list[FiredRuleRead]


class CurvePoint(CamelModel):
    x: float
    degree: float = Field(ge=0, le=1)


class BiomarkerDefRead(CamelModel):
    key: BiomarkerKey
    label: str
    short_label: str
    unit: str
    normal_range: tuple[float, float]
    lower_is_worse: bool
    description: str
    # Pre-sampled (x, degree) points per fuzzy set (low/borderline/normal[/high]),
    # so the frontend only ever plots numbers the backend computed — see
    # `BiomarkerDef.sample_curve()`'s docstring for why this exists.
    curve: dict[str, list[CurvePoint]]


class FISSimulateRequest(CamelModel):
    """Lets a clinician (or the frontend's "what-if" exploration) run the
    fuzzy inference engine directly on hypothetical biomarker values,
    without a scan or a persisted patient — useful for sanity-checking rule
    behavior and for demoing the engine in isolation.
    """

    hippocampal_volume: float = Field(gt=0, le=15, description="mL")
    ventricle_brain_ratio: float = Field(ge=0, le=10, description="%")
    cortical_thickness: float = Field(gt=0, le=15, description="mm")


class RuleDescriptionRead(CamelModel):
    id: str
    antecedent: str
    consequent: Stage
