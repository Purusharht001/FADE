# FADE Backend

The API behind [FADE](../README.md)'s decision-support dashboard: ingestion of volumetric MRI
biomarkers, a fuzzy inference engine that stages them, and the REST API the dashboard and the
team's tools talk to.

It handles two kinds of data, and keeps them apart:

- **Synthetic phantom data**, measured by an in-house volumetry pipeline. This is what the demo
  runs on.
- **Real data**, measured by FreeSurfer or FastSurfer: the OASIS-1 cohort, and anonymised scans
  from the clinic.

**Real data is stored and scored, but not yet staged.** The only calibration that exists was
fitted to the phantom, and the engine refuses to apply it to real measurements. A real scan comes
back "indeterminate" until a real calibration file is supplied.
[`docs/REAL_DATA_RUNBOOK.md`](../docs/REAL_DATA_RUNBOOK.md) is the step-by-step guide for
importing the cohort, processing a clinic scan and installing that file.

## Two routes to the same engine

```
Phantom   NIfTI ──> preprocessing ──> volumetry (in-house) ──┐
                                                             ├──> calibration ──> fuzzy engine ──> result
Real      FreeSurfer / FastSurfer stats ──> derivation ──────┘
```

**Phantom route.** [`app/services/synthetic_mri.py`](app/services/synthetic_mri.py) generates a
parametric synthetic brain MRI (a labelled phantom with hippocampus, ventricles and a cortical
ribbon, driven by one 0 to 1 "severity" value). It goes through real preprocessing
(`preprocessing.py`) and a phantom-tuned segmentation (`volumetry.py`). That volumetry only
understands the phantom's 64x64x64 grid and is refused for any patient not labelled `PHANTOM`.

**Real route.** FADE does not segment real scans itself. FreeSurfer or FastSurfer does, and FADE
reads the resulting stats files (`freesurfer_stats.py`) and derives the engine's three inputs from
them (`biomarker_derivation.py`):

| Input | Formula | Unit |
|---|---|---|
| Hippocampal volume | (left + right hippocampus) / ICV x 1000 | `‰ICV` |
| Ventricle-to-brain ratio | (left + right lateral + left + right inferior lateral ventricles) / BrainSegVol | `ratio` |
| Cortical thickness | mean `ThickAvg` over entorhinal, parahippocampal, superior, middle and inferior temporal, both hemispheres | `mm` |

Values outside physiological plausibility are flagged, not rejected, and a flagged scan is never
staged.

## Architecture

```
app/
├── main.py                 FastAPI app: middleware, exception handlers, startup checks
├── calibration/            versioned calibration files: every number the engine uses
│   ├── phantom.v0.json       fitted to the synthetic phantom (the default)
│   └── real.template.json    same structure, every number null, for the ML team to fill in
├── cli/                    command-line tools: import_cohort, rescore, create_user
├── core/                   settings, logging, JWT/password security, exceptions, rate limiting,
│                           startup checks
├── db/                     async engine/session, declarative base, demo seeding, schema check
├── models/                 ORM: User, Patient, Scan, BiomarkerReading, FISResult,
│                           ClinicianRating, AuditLog
├── schemas/                Pydantic request/response models (camelCase on the wire)
├── repositories/           query layer: the only place raw SQLAlchemy queries live
├── services/
│   ├── freesurfer_stats.py     parser for aseg / aparc stats, FreeSurfer and FastSurfer
│   ├── biomarker_derivation.py parsed volumes -> the three engine inputs, with units and QC
│   ├── upload_validation.py    upload storage, file type by content, NIfTI sanitising
│   ├── dicom_ingest.py         DICOM zip: de-identification gate, conversion, deletion
│   ├── volumetry_providers.py  external / fastsurfer_docker / inhouse, background job
│   ├── calibration.py          loads and validates calibration files
│   ├── biomarkers.py           trapezoidal membership functions, biomarker structure
│   ├── fis_engine.py           rule base, inference, indeterminate results, defuzzification
│   ├── pipeline.py             measurement -> calibration -> engine
│   ├── scan_service.py         ties the pipeline to persistence
│   ├── cohort_import.py        OASIS cohort import
│   ├── rescore.py              re-run the engine on stored scans
│   ├── export_service.py       research export
│   ├── audit.py                audit log
│   └── synthetic_mri.py, preprocessing.py, volumetry.py   the phantom route
└── api/v1/                 routers: auth, patients, scans, fis, cohort, study, export, health
```

Each layer only talks to the one below it: routers depend on services and repositories, services
never import routers, repositories are the only place SQL happens.
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) has a per-file table generated from the import
graph.

Every schema in `app/schemas/` inherits from `CamelModel`: the wire format is camelCase JSON,
while Python code keeps snake_case. Request bodies accept either casing; responses are always
camelCase.

## The fuzzy inference engine

[`app/services/fis_engine.py`](app/services/fis_engine.py), in the order it works:

1. **Preconditions.** The engine checks it is entitled to an opinion: the calibration is
   complete and was fitted to this kind of data, all three biomarkers are present in the units
   the calibration expects, and none carries a plausibility flag. If not, the result is
   **indeterminate**: `stage` is null, `needsReview` is true, and `indeterminateReason` says why.
2. **Fuzzification.** Each biomarker is fuzzified against its own trapezoidal sets, so
   hippocampal volume can be "a little atrophied" (0.3) and "mostly normal" (0.7) at once.
3. **Rules.** Ten explicit rules combine these with fuzzy AND (min) and OR (max), for example
   *"IF hippocampus is atrophied AND ventricle-to-brain ratio is enlarged THEN AD"*. A stage's
   activation is the strongest rule concluding it. If no rule fires, the result is indeterminate.
4. **Stage, confidence, uncertainty.** The three activations are normalised to sum to 1. The
   stage is the largest share, `confidence` is that share, and `uncertainty` is how close the
   runner-up is. The triage dashboard sorts by uncertainty. These are shares of rule activation,
   not probabilities.
5. **Severity.** Mamdani centroid defuzzification: each stage's output set is clipped at its
   activation, the clipped sets are combined with max, and the centroid on a 0 to 1 axis is
   reported as `severity`. The output sets come from the calibration file. The ones shipped are
   placeholders marked provisional, so `severity` has the right mechanics and no clinical meaning
   yet.

The rule base and every breakpoint are the team's literature-informed starting point. They have
not been reviewed by a neurologist. The open assumptions are tracked as C1 to C10 in
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#7-clinical-assumption-register).

`GET /api/v1/fis/rules` lists the live rule base, and `POST /api/v1/fis/simulate` runs the engine
on values you supply with no scan or patient involved.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # defaults to local SQLite, no external services needed
uv sync                       # installs deps + creates .venv
uv run alembic upgrade head   # or: make migrate
uv run python scripts/seed.py # demo cohort + clinician login, or: make seed
uv run uvicorn app.main:app --reload   # or: make dev
```

Then open `http://127.0.0.1:8000/docs` for interactive OpenAPI docs. Demo login (from the seed
script): `clinician@fade.demo` / `fade-demo-2026`.

A `Makefile` wraps the common commands: `make dev`, `make test`, `make lint`, `make migrate`,
`make seed`, `make docker-up`.

**After pulling new code, run `uv run alembic upgrade head` before starting the server.** The
server checks the schema at startup and refuses to start against a database that is behind,
naming that command.

## Database

Defaults to a local SQLite file (`DATABASE_URL=sqlite+aiosqlite:///./fade.db`). If that file does
not exist yet, the server creates the schema on first start and stamps it with the current
Alembic revision, so later migrations apply to it normally. PostgreSQL (used by
`docker-compose.yml`) is always managed by migrations:

```bash
uv run alembic revision --autogenerate -m "description"   # after changing models
uv run alembic upgrade head
```

When you add a migration, update `SCHEMA_HEAD` in `app/db/schema.py`; a test fails until you do.
Migrations must work on both SQLite and PostgreSQL: use `op.batch_alter_table` to alter a column.

## Command-line tools

Run from `backend/`. Each has `--help`. Inside the Docker image, which does not ship `scripts/`,
use `python -m app.cli.<name>`.

| Command | Purpose |
|---|---|
| `uv run python scripts/import_cohort.py` | Import a cohort from a biomarker CSV, or from FreeSurfer stats folders plus the OASIS demographics sheet. Rows with no CDR are skipped and counted, never defaulted. `--dry-run`, `--require-label`, `--include-mr2`. |
| `uv run python scripts/rescore.py` | Re-run the engine on stored scans after `CALIBRATION_FILE` changes. `--dry-run`, `--source`. |
| `uv run python scripts/create_user.py` | Create a clinician, researcher or admin account, or change a role. Prompts for the password. |
| `uv run python scripts/seed.py` | Seed the synthetic demo cohort. Every seeded patient is labelled `PHANTOM`. |
| `uv run python scripts/gen_architecture_table.py` | Regenerate the file table in `docs/ARCHITECTURE.md`. |

## API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/login`, `/refresh` | JWT access + refresh tokens |
| POST | `/api/v1/auth/register` | Creates a clinician. Closed (403) when `AUTH_MODE=required` |
| GET | `/api/v1/auth/me` | Current user |
| GET | `/api/v1/patients` | Triage list, sorted by uncertainty by default; filter by `stage` / `needsReview` |
| POST | `/api/v1/patients` | Create a patient record (`source`: `Clinic`, `OASIS`, `ADNI`, `PHANTOM`) |
| GET | `/api/v1/patients/{id}` | Full detail including scan history |
| POST | `/api/v1/patients/{id}/scans/upload` | Upload one NIfTI volume. What happens next depends on `VOLUMETRY_PROVIDER` |
| POST | `/api/v1/patients/{id}/scans/upload-dicom` | Upload a zip of an anonymised DICOM series |
| POST | `/api/v1/patients/{id}/scans/freesurfer` | Create a scan from stats files: `aseg` (required), `lh_aparc` + `rh_aparc` (optional) |
| POST | `/api/v1/patients/{id}/scans/biomarkers` | Create a scan from raw volumes or derived values, as JSON |
| POST | `/api/v1/patients/{id}/scans/{scan_id}/stats` | Attach stats to a scan that is `awaiting_volumetry` |
| POST | `/api/v1/patients/{id}/scans/synthetic` | Demo only: generate and process a phantom scan |
| GET | `/api/v1/patients/{id}/scans/{scan_id}` | Scan detail; poll this for a scan that is `segmenting` |
| POST | `/api/v1/patients/{id}/scans/{scan_id}/review` | Mark or unmark clinician-reviewed |
| GET | `/api/v1/fis/rules`, `/biomarkers` | The live rule base and the active calibration's biomarker definitions |
| POST | `/api/v1/fis/simulate` | Run the engine on hypothetical values, no persistence |
| GET | `/api/v1/cohort/stats` | Dashboard summary |
| GET | `/api/v1/study/cases` | Blind rating study: cases as biomarkers only. Clinicians |
| POST | `/api/v1/study/cases/{scan_id}/rating` | Record or replace the caller's rating. Clinicians |
| GET | `/api/v1/export/results.csv` | Research export, one row per scan. Researchers and admins |
| GET | `/api/v1/health`, `/health/db` | Liveness / DB connectivity |

Full request/response schemas: `/docs` (Swagger UI) or `/redoc`.

### What changed in the contract

Nothing was renamed or removed. Additions:

- `fisResult.determinate`, `indeterminateReason`, `severity`, `calibrationVersion`.
- `fisResult.stage` and `biomarkers[].abnormality` can now be `null`.
- Scan: `extractionMethod`, `externalSubjectId`, `rawVolumes`, `qcFlags`, `calibrationVersion`,
  `engineVersion`. Scan statuses `awaiting_volumetry` and `segmenting`.
- Patient `source` can be `PHANTOM`. Cohort stats have `indeterminate`.
- A 422 for an invalid request body no longer repeats the rejected input back.

The reference label (ground-truth CDR) is stored on a scan and returned only by the export.

## Settings

All settings are environment variables, documented with their defaults in
[`.env.example`](.env.example). The ones that matter for real data:

| Setting | Default | Effect |
|---|---|---|
| `AUTH_MODE` | `required` in production, else `demo` | `demo`: a request with no token acts as a shared demo clinician. `required`: every request needs a token. `demo` in production stops the server from starting |
| `ALLOW_SELF_REGISTRATION` | open in demo, closed when required | Whether `POST /auth/register` works |
| `SECRET_KEY` | a development placeholder | JWT signing key. The placeholder stops the server from starting in production |
| `CALIBRATION_FILE` | `phantom.v0.json` | The calibration file. A bare name is looked up in `app/calibration/` |
| `VOLUMETRY_PROVIDER` | `external` | What happens to an uploaded scan: `external`, `fastsurfer_docker` or `inhouse` |
| `REQUIRE_ANONYMISED_DICOM` | `true` | Reject a DICOM whose identifying tags are not blank |
| `MAX_UPLOAD_MB` | `64` | Upload size limit |
| `RATE_LIMIT_DEFAULT`, `UPLOAD_RATE_LIMIT` | `120/minute`, `20/minute` | Per client address and route |
| `PLAUSIBILITY_ICV_MM3`, `PLAUSIBILITY_HIPPOCAMPUS_TOTAL_MM3`, `PLAUSIBILITY_THICKNESS_MM` | see `.env.example` | Bounds outside which a value is QC-flagged |
| `CORTICAL_THICKNESS_REGIONS`, `CORTICAL_THICKNESS_WEIGHTING` | five temporal regions, `unweighted` | How cortical thickness is derived from `aparc` stats |
| `FS_LICENSE_PATH`, `FASTSURFER_*` | see `.env.example` | Used by the `fastsurfer_docker` provider only |

## Testing

```bash
make test        # pytest + coverage
make lint        # ruff
make typecheck   # mypy
```

782 tests. The suite runs against an isolated in-memory SQLite database per test. CI also runs
`alembic upgrade head` against a real PostgreSQL container, since migrations can behave
differently there.

What is covered, beyond the happy path:

- **Parsers and derivations:** hand-written stats files in FreeSurfer, older-FreeSurfer and
  FastSurfer layouts; missing structures, missing ICV, unparseable lines, zero and negative
  volumes; the empty-symlink case on Windows.
- **The engine contract:** golden values captured from the engine before this work, to prove
  that staged results did not change; every indeterminate path; centroid defuzzification.
- **Uploads:** file type by content, the size limit, path traversal in filenames and zip
  entries, the DICOM de-identification gate, and what is left on disk afterwards.
- **The cohort import:** in particular that a missing CDR is never turned into a 0.
- **Migrations:** run for real against a throwaway SQLite file, including the corrections to
  existing rows.
- **Security:** required-auth mode on every route, roles, rate limits, the checks that stop the
  server starting.

No real subject data is in the repository. Fixtures under `tests/fixtures/` are hand-written
with invented numbers, and DICOM series are synthesised by the tests. One test is skipped: it
checks the VBR formula against a real OASIS subject and runs only when that subject's stats file
is placed in the git-ignored `tests/fixtures/private/`.

Not exercised by the suite, because the tools were not available: FastSurfer in a real Docker
container, `dcm2niix`, and PostgreSQL locally. See section F of the runbook.

## Docker

The full-stack `docker-compose.yml` (Postgres + this API + the React frontend) lives at the
**repo root**; see [`../README.md`](../README.md#12-running-it). This backend also has its own
`Dockerfile` (multi-stage, runs `docker-entrypoint.sh` to apply migrations before starting
`uvicorn`):

```bash
docker build -t fade-backend .
```

The compose file runs the API with `ENVIRONMENT=production`, which requires a token on every
request. The frontend has no login screen yet, so a full-stack demo needs
`ENVIRONMENT=development` in the root `.env`. The `fastsurfer_docker` provider does not work from
inside the API container, because it starts Docker itself.

## Security notes

- **Authentication.** `AUTH_MODE=required` rejects any request without a valid bearer token.
  `AUTH_MODE=demo` lets a request with no token act as one shared demo clinician; it is for
  synthetic data only, logs a warning at startup, and cannot be combined with
  `ENVIRONMENT=production`. Role checks apply in both modes: the export needs a researcher or
  admin, and rating needs a clinician.
- **Accounts.** Self-registration is closed when auth is required. Create accounts with
  `scripts/create_user.py`.
- **Uploads.** File type is decided from content. Files are stored under random names in
  `UPLOAD_DIR`, which the server refuses to serve as static files. `data/uploads/` is
  git-ignored.
- **Patient information.** There is no name field anywhere. DICOM with identifying tags is
  rejected and the DICOM is deleted after conversion. NIfTI headers are rebuilt on the way in.
  Error messages and logs do not include file contents or header values. The audit log accepts
  only numbers and short codes. Two things the server cannot check: free-text rating notes, and
  whether a research subject code identifies someone.
- **Audit.** Review sign-off, ratings, exports, stats attachment, cohort imports, re-scoring and
  account changes are recorded in `audit_log`.
- **Rate limiting** is per client address, held in process memory. With several worker
  processes each keeps its own count.
- CORS is locked to the configured frontend origins.

Known gap: the blind rating study hides the engine's output from the study endpoints, but the
same clinician account can still open the triage endpoints. See the runbook, section E.
