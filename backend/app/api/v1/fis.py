from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.core.exceptions import CalibrationIncompleteError
from app.models.enums import BiomarkerKey
from app.schemas.fis import (
    BiomarkerDefRead,
    CurvePoint,
    FiredRuleRead,
    FISResultRead,
    FISSimulateRequest,
    RuleDescriptionRead,
)
from app.services.calibration import get_calibration
from app.services.fis_engine import RULES, run_fis

router = APIRouter(prefix="/fis", tags=["fis"])


@router.get("/biomarkers", response_model=list[BiomarkerDefRead])
async def list_biomarker_defs(_current_user: CurrentUser) -> list[BiomarkerDefRead]:
    """The biomarker definitions of the active calibration file."""
    calibration = get_calibration()
    if not calibration.complete:
        raise CalibrationIncompleteError(
            f"The active calibration ({calibration.version}) has unset values, so there are "
            "no biomarker definitions to show. Set CALIBRATION_FILE to a complete file."
        )
    return [
        BiomarkerDefRead(
            key=d.key,
            label=d.label,
            short_label=d.short_label,
            unit=d.unit,
            normal_range=d.normal_range,
            lower_is_worse=d.lower_is_worse,
            description=d.description,
            curve={
                label.value: [CurvePoint(x=x, degree=degree) for x, degree in points]
                for label, points in d.sample_curve().items()
            },
        )
        for d in calibration.biomarkers.values()
    ]


@router.get("/rules", response_model=list[RuleDescriptionRead])
async def list_rules(_current_user: CurrentUser) -> list[RuleDescriptionRead]:
    """Introspects the live rule base — what a clinician sees here is
    guaranteed to be exactly what's executing, not documentation that's
    drifted from the code.
    """
    return [
        RuleDescriptionRead(id=r.id, antecedent=r.antecedent_text, consequent=r.consequent)
        for r in RULES
    ]


@router.post("/simulate", response_model=FISResultRead)
async def simulate(payload: FISSimulateRequest, _current_user: CurrentUser) -> FISResultRead:
    """Runs the fuzzy inference engine directly on hypothetical biomarker
    values — no scan, no persistence. Lets a clinician sanity-check rule
    behavior at the boundaries, and lets the frontend offer a "what-if"
    slider explorer without needing a real patient record.
    """
    settings = get_settings()
    values = {
        BiomarkerKey.HIPPOCAMPAL_VOLUME: payload.hippocampal_volume,
        BiomarkerKey.VENTRICLE_BRAIN_RATIO: payload.ventricle_brain_ratio,
        BiomarkerKey.CORTICAL_THICKNESS: payload.cortical_thickness,
    }
    output = run_fis(values, review_threshold=settings.uncertainty_review_threshold)
    return FISResultRead(
        stage=output.stage,
        confidence=output.confidence,
        uncertainty=output.uncertainty,
        needs_review=output.needs_review,
        determinate=output.determinate,
        indeterminate_reason=output.indeterminate_reason,
        severity=output.severity,
        calibration_version=output.calibration_version,
        membership=output.membership,
        fired_rules=[
            FiredRuleRead(
                id=r.id,
                antecedent=r.antecedent,
                consequent=r.consequent,
                firing_strength=r.firing_strength,
            )
            for r in output.fired_rules
        ],
    )
