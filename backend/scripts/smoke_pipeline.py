"""One-off manual smoke test for the synthetic MRI -> FIS pipeline. Not part
of the pytest suite (see tests/test_pipeline.py for the real coverage) —
just a fast way to eyeball severity-vs-output behavior end-to-end.
"""

from app.services.pipeline import run_pipeline_synthetic

for severity in (0.0, 0.15, 0.3, 0.45, 0.5, 0.55, 0.7, 0.85, 1.0):
    result = run_pipeline_synthetic(severity, seed=42)
    fis = result.fis_output
    membership = {k.value: round(v, 2) for k, v in fis.membership.items()}
    print(
        f"severity={severity:.2f}  "
        f"HV={result.biomarkers['hippocampal_volume']:.2f}mL  "
        f"VBR={result.biomarkers['ventricle_brain_ratio']:.2f}%  "
        f"CT={result.biomarkers['cortical_thickness']:.2f}mm  "
        f"-> stage={fis.stage.value} conf={fis.confidence:.1f}% unc={fis.uncertainty:.1f}% "
        f"review={fis.needs_review} membership={membership}"
    )
