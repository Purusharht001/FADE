"""Regenerate the backend file-reference table in docs/ARCHITECTURE.md.

The import graph is derived from the AST rather than maintained by hand, so
the table cannot drift from the code. Responsibilities are authored here --
add an entry when you add a module; the script fails loudly on any file it
does not have a description for.

Usage (from backend/):  uv run python scripts/gen_architecture_table.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections import defaultdict

APP = pathlib.Path("app")
DOC = pathlib.Path("../docs/ARCHITECTURE.md")
START = "<!-- BEGIN GENERATED: backend-file-reference -->"
END = "<!-- END GENERATED: backend-file-reference -->"

RESPONSIBILITIES: dict[str, str] = {
    "app/__init__.py": "Package marker. No logic.",
    "app/api/deps.py": (
        "Shared FastAPI dependencies: DB session, current-user resolution under `AUTH_MODE`, "
        "role checks."
    ),
    "app/api/v1/auth.py": (
        "Login / token issue / refresh endpoints. Registration is closed when auth is required."
    ),
    "app/api/v1/cohort.py": "Cohort-level aggregate endpoints (stage distribution, counts).",
    "app/api/v1/export.py": (
        "Research export (`/export/results.csv`), researcher/admin only. The only endpoint "
        "that returns the reference label."
    ),
    "app/api/v1/fis.py": (
        "Direct fuzzy-inference endpoints: `/fis/simulate` what-if, biomarker "
        "definitions, rule list."
    ),
    "app/api/v1/health.py": "Liveness/readiness and app metadata.",
    "app/api/v1/patients.py": "Patient CRUD and listing.",
    "app/api/v1/router.py": "Mounts every v1 sub-router under the configured prefix.",
    "app/api/v1/scans.py": (
        "Scan ingestion (NIfTI upload, DICOM zip, FreeSurfer stats, manual biomarkers, stats "
        "attachment, synthetic), scan read, clinician review."
    ),
    "app/api/v1/study.py": (
        "Blind clinician-rating study: cases as biomarkers only, one rating per rater per scan."
    ),
    "app/cli/create_user.py": "Command line: create a user or change a role.",
    "app/cli/import_cohort.py": "Command line: import a cohort CSV or a FreeSurfer stats folder.",
    "app/cli/rescore.py": "Command line: re-score stored scans under the active calibration.",
    "app/core/config.py": (
        "Pydantic `Settings`, env-sourced. Refuses demo auth or the placeholder secret in "
        "production. Holds `uncertainty_review_threshold` - see C6."
    ),
    "app/core/exceptions.py": (
        "Domain exception types and their HTTP mappings; validation errors without input echo."
    ),
    "app/core/identifiers.py": "Shape check for research subject identifiers.",
    "app/core/logging.py": "structlog configuration and logger factory.",
    "app/core/rate_limit.py": "Per-client rate limiter and the default-limit ASGI middleware.",
    "app/core/security.py": "Password hashing (bcrypt) and JWT encode/decode.",
    "app/core/startup_checks.py": (
        "Startup checks: uploads directory not static-served, demo-mode warning."
    ),
    "app/db/base.py": "Declarative `Base` plus UUID-PK and timestamp mixins.",
    "app/db/schema.py": (
        "Startup schema check: creates and stamps an empty SQLite DB, refuses a stale one."
    ),
    "app/db/seed.py": "Deterministic demo seeding. Every seeded patient is labelled PHANTOM.",
    "app/db/session.py": "Async engine and sessionmaker.",
    "app/main.py": "App factory: middleware, exception handlers, router mount, lifespan.",
    "app/middleware/logging.py": (
        "Per-request structured access logging. Only direct consumer of `starlette`."
    ),
    "app/models/__init__.py": "Imports every model so Alembic autogenerate sees them.",
    "app/models/audit.py": "`AuditLog` ORM entity. Append-only, no free-text column.",
    "app/models/enums.py": (
        "`Stage`, `BiomarkerKey`, `ScanStatus`, `DataSource` (incl. `PHANTOM`), "
        "`ExtractionMethod`, `Sex`, `UserRole`."
    ),
    "app/models/patient.py": "Patient ORM entity.",
    "app/models/scan.py": (
        "`Scan` (with provenance and the research-only reference label), `BiomarkerReading`, "
        "`FISResult` ORM entities."
    ),
    "app/models/study.py": "`ClinicianRating` ORM entity.",
    "app/models/user.py": "Clinician / researcher / admin user entity.",
    "app/repositories/patient_repo.py": "Patient and scan persistence queries.",
    "app/repositories/study_repo.py": "Queries for the rating study and the research export.",
    "app/repositories/user_repo.py": "User lookup and creation.",
    "app/schemas/auth.py": "Login/token request and response models. Uses `EmailStr`.",
    "app/schemas/base.py": "`CamelModel` - the camelCase JSON contract base.",
    "app/schemas/biomarkers.py": "Request models for biomarkers measured outside this service.",
    "app/schemas/cohort.py": "Cohort summary response models.",
    "app/schemas/fis.py": "FIS result, fired rule, biomarker definition and curve models.",
    "app/schemas/patient.py": "Patient request/response models.",
    "app/schemas/scan.py": "Scan request/response models.",
    "app/schemas/study.py": "Blinded study case and rating models.",
    "app/services/audit.py": (
        "Writes audit entries; refuses any detail value that is not a number or a code."
    ),
    "app/services/biomarker_derivation.py": (
        "Pure derivations from parsed volumes to the three engine inputs, with units and "
        "plausibility QC flags. `compute_vbr` lives here."
    ),
    "app/services/biomarkers.py": (
        "`trapmf`, `BiomarkerDef`, the structural half of each biomarker. No numbers: those "
        "are in `app/calibration/`. **C7 lives here.**"
    ),
    "app/services/calibration.py": (
        "Loads and validates calibration JSON; data-domain gating. **C3, C8 values are in "
        "`app/calibration/phantom.v0.json`.**"
    ),
    "app/services/cohort_import.py": (
        "Cohort import: CSV or stats folder plus demographics. Never defaults a missing CDR."
    ),
    "app/services/dicom_ingest.py": (
        "DICOM zip: safe unpack, de-identification gate, conversion to NIfTI, deletion."
    ),
    "app/services/export_service.py": "Builds the research export rows and renders CSV.",
    "app/services/fis_engine.py": (
        "Rule base, inference, indeterminate results, centroid defuzzification. "
        "**C5 lives here.**"
    ),
    "app/services/freesurfer_stats.py": "Parser for FreeSurfer / FastSurfer aseg and aparc stats.",
    "app/services/pipeline.py": (
        "Orchestrates measurement -> calibration -> inference for file, synthetic and "
        "externally measured inputs."
    ),
    "app/services/preprocessing.py": "NIfTI load, bias correction, normalisation, brain masking.",
    "app/services/rescore.py": (
        "Re-runs the engine on stored biomarker readings after the calibration changes."
    ),
    "app/services/scan_service.py": (
        "Persists scan runs and their biomarker/FIS results; drives status transitions; keeps "
        "phantom and real data on separate patients."
    ),
    "app/services/synthetic_mri.py": (
        "The phantom generator. Regression fixture - must not be removed."
    ),
    "app/services/upload_validation.py": (
        "Upload storage with size limit, file type by magic bytes, NIfTI sanitising."
    ),
    "app/services/volumetry.py": (
        "Phantom-tuned tissue segmentation and biomarker extraction. **C9 lives here.**"
    ),
    "app/services/volumetry_providers.py": (
        "Volumetry providers (external, fastsurfer_docker, inhouse) and the FastSurfer "
        "background job."
    ),
}


def module_name(path: pathlib.Path) -> str:
    dotted = str(path.with_suffix("")).replace("\\", ".").replace("/", ".")
    return dotted.removesuffix(".__init__")


def build_graph(
    files: list[pathlib.Path],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    known = {module_name(p): p for p in files}
    imports: dict[str, set[str]] = defaultdict(set)
    imported_by: dict[str, set[str]] = defaultdict(set)

    for path in files:
        me = module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                # Consider `from x.y import z` as a possible reference to
                # module x.y.z as well as to x.y itself.
                targets = [f"{node.module}.{a.name}" for a in node.names] + [node.module]

            for target in targets:
                if not target.startswith("app"):
                    continue
                candidates = [c for c in known if target == c or target.startswith(c + ".")]
                if not candidates:
                    continue
                # Longest match wins: `app.api.v1.scans` must not collapse to `app`.
                base = max(candidates, key=len)
                if base != me:
                    imports[me].add(base)
                    imported_by[base].add(me)

    return imports, imported_by


def render_cell(modules: set[str]) -> str:
    trimmed = sorted(m.removeprefix("app.") for m in modules if m != "app")
    return ", ".join(f"`{m}`" for m in trimmed) or "-"


def main() -> int:
    files = sorted(APP.rglob("*.py"))
    imports, imported_by = build_graph(files)

    unmapped = [
        str(p).replace("\\", "/")
        for p in files
        if str(p).replace("\\", "/") not in RESPONSIBILITIES
    ]
    if unmapped:
        print("No responsibility described for:", *unmapped, sep="\n  ", file=sys.stderr)
        return 1

    lines = ["| File | Responsibility | Imports (internal) | Imported by |", "|---|---|---|---|"]
    for path in files:
        key = str(path).replace("\\", "/")
        name = module_name(path)
        lines.append(
            f"| `{key}` | {RESPONSIBILITIES[key]} | "
            f"{render_cell(imports[name])} | {render_cell(imported_by[name])} |"
        )
    table = "\n".join(lines)

    doc = DOC.read_text(encoding="utf-8")
    if START not in doc or END not in doc:
        print(f"Markers {START} / {END} not found in {DOC}", file=sys.stderr)
        return 1
    head, _, rest = doc.partition(START)
    _, _, tail = rest.partition(END)
    DOC.write_text(f"{head}{START}\n{table}\n{END}{tail}", encoding="utf-8")

    print(f"Regenerated {len(files)} rows in {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
