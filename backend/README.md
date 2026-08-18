# FADE Backend

The API behind [FADE](../README.md)'s decision-support dashboard — MRI preprocessing/volumetry
pipeline, a Mamdani-style fuzzy inference engine, and the clinician-facing REST API. Implements
**Phases 2–6** of the project [roadmap](../PHASES.md): preprocessing, volumetric feature extraction,
fuzzy inference system design, system integration, and (partially) benchmark validation.

## Why a synthetic MRI phantom

Phase 1 (dataset access, clinic consent) is still open — there is no real OASIS/ADNI/clinic imaging
data wired into this repository. Rather than leave the pipeline as an untested stub,
[`app/services/synthetic_mri.py`](app/services/synthetic_mri.py) generates a parametric synthetic
brain MRI (a labeled phantom with hippocampus, ventricles, and a cortical ribbon, driven by a single
0–1 "severity" knob) and every request goes through the **exact same** preprocessing → segmentation →
biomarker extraction → fuzzy inference code a real scan would. When real data lands, only
`synthetic_mri.py` and the calibration constants in `volumetry.py`/`biomarkers.py` need replacing —
the pipeline shape, the API, and the fuzzy inference engine do not change. See each module's docstring
for the specific swap-in points (FreeSurfer/FSL for segmentation, N4ITK for bias correction, etc.).

## Architecture

```
app/
├── main.py                 FastAPI app: middleware, exception handlers, lifespan
├── core/                   config (pydantic-settings), logging (structlog), JWT/password security,
│                           domain exceptions
├── db/                     async SQLAlchemy engine/session, declarative base, demo-data seeding
├── models/                 SQLAlchemy ORM: User, Patient, Scan, BiomarkerReading, FISResult
├── schemas/                Pydantic request/response models
├── repositories/           query layer — the only place raw SQLAlchemy queries live
├── services/
│   ├── biomarkers.py         biomarker definitions + trapezoidal fuzzy sets
│   ├── fis_engine.py          the fuzzy inference engine itself (rule base, Mamdani aggregation)
│   ├── synthetic_mri.py       parametric phantom MRI generator (see above)
│   ├── preprocessing.py       skull-strip, bias-field correction, intensity normalization
│   ├── volumetry.py           tissue segmentation + biomarker extraction
│   ├── pipeline.py            orchestrates preprocessing → volumetry → FIS
│   └── scan_service.py        ties the pipeline to persistence
└── api/v1/                 routers: auth, patients, scans, fis, cohort, health
```

Each layer only talks to the one below it: routers depend on services + repositories, services never
import routers, repositories are the only place SQL happens. `pipeline.py` and `scan_service.py` are
the seam between "pure algorithm" (testable with zero I/O) and "persisted to a database."

Every schema in `app/schemas/` inherits from `CamelModel` (`app/schemas/base.py`): the wire format is
camelCase JSON (idiomatic for the TypeScript frontend), while Python code — construction,
`.model_validate()` from ORM objects, attribute access — keeps using normal snake_case throughout.
Request bodies accept either casing (`populate_by_name=True`); responses always serialize camelCase.

## The fuzzy inference engine

[`app/services/fis_engine.py`](app/services/fis_engine.py) is a genuine multi-antecedent Mamdani
system, not a single-axis approximation:

1. Each of the three biomarkers is fuzzified independently against its own trapezoidal membership
   functions (`biomarkers.py`) — e.g. hippocampal volume is simultaneously "a little atrophied" (0.3)
   and "a little normal" (0.7), not forced into one bucket.
2. Ten explicit rules combine these with fuzzy AND (min) / OR (max) — e.g. *"IF hippocampus is
   atrophied AND ventricle-to-brain ratio is enlarged THEN AD"* — each producing a firing strength.
3. Per-stage activation is the max firing strength across every rule concluding that stage (standard
   Mamdani aggregation), then normalized into a membership distribution.
4. The winning stage's share is the reported confidence; the gap to the runner-up drives the
   **uncertainty** score — a close CN/MCI call surfaces as high uncertainty, which is what the triage
   dashboard actually sorts by.

`GET /api/v1/fis/rules` introspects the *live* rule base (not documentation that can drift), and
`POST /api/v1/fis/simulate` runs it on arbitrary biomarker values with no scan or patient required —
useful for sanity-checking rule behavior at the boundaries.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # defaults to local SQLite, no external services needed
uv sync                       # installs deps + creates .venv
uv run alembic upgrade head   # or: make migrate
uv run python scripts/seed.py # demo cohort + clinician login — or: make seed
uv run uvicorn app.main:app --reload   # or: make dev
```

Then open `http://127.0.0.1:8000/docs` for interactive OpenAPI docs. Demo login (from the seed
script): `clinician@fade.demo` / `fade-demo-2026`.

A `Makefile` wraps the common commands: `make dev`, `make test`, `make lint`, `make migrate`,
`make seed`, `make docker-up`.

## Database

Defaults to a local SQLite file (`DATABASE_URL=sqlite+aiosqlite:///./fade.db`) — zero external
dependencies for local dev. In that mode the app auto-creates tables on startup. For Postgres
(`docker-compose.yml` uses it), migrations are tracked properly:

```bash
uv run alembic revision --autogenerate -m "description"   # after changing models
uv run alembic upgrade head
```

## Testing

```bash
make test   # pytest + coverage
make lint   # ruff
make typecheck   # mypy
```

The suite itself runs against an isolated in-memory SQLite database per test (fast, zero service
dependencies); CI additionally runs `alembic upgrade head` against a real ephemeral Postgres container
as a separate smoke-test step, since that's the engine production actually uses and migrations can
behave differently there (enum/JSON column handling, type mapping) than they do on SQLite.

99 tests, including dedicated suites for the three modules doing the actual image processing —
`test_preprocessing.py`, `test_volumetry.py`, `test_scan_service.py` — covering corrupted/truncated
NIfTI files, empty segmentations, and pathological-contrast volumes (uniform intensity, pure noise),
not just the happy path. Two real bugs were caught and fixed by writing these: a bias-field-correction
edge artifact that spiked intensity right at the mask boundary, and pure noise passing skull-strip
entirely (a random-noise mask sits above the 3D percolation threshold and forms one large *connected*
component, so the size/count checks alone didn't catch it — `skull_strip` now also rejects a mask that
isn't compact enough to be brain-shaped; see its docstring). Also: the fuzzy inference engine's rule
logic and normalization invariants, biomarker fuzzification edge cases (including a degenerate-boundary
regression), and API integration tests against an isolated in-memory database per test.

## Docker

The full-stack `docker-compose.yml` (Postgres + this API + the React frontend) lives at the **repo
root**, not here — see [`../README.md`](../README.md#docker-full-stack) for the `docker compose up`
instructions. This backend also has its own standalone `Dockerfile` (multi-stage, runs
`docker-entrypoint.sh` to apply migrations before starting `uvicorn`) if you want to build/run just
the API image directly:

```bash
docker build -t fade-backend .
```

## API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/register`, `/login`, `/refresh` | JWT access + refresh tokens |
| GET | `/api/v1/auth/me` | current user |
| GET | `/api/v1/patients` | triage list, sorted by uncertainty by default; filter by `stage`/`needs_review` |
| POST | `/api/v1/patients` | create a patient record |
| GET | `/api/v1/patients/{id}` | full detail incl. scan history |
| POST | `/api/v1/patients/{id}/scans/upload` | upload a real `.nii`/`.nii.gz` file, runs the full pipeline |
| POST | `/api/v1/patients/{id}/scans/synthetic` | demo-only: generate + process a synthetic scan at a given severity |
| GET | `/api/v1/patients/{id}/scans/{scan_id}` | scan detail incl. biomarkers + FIS result |
| POST | `/api/v1/patients/{id}/scans/{scan_id}/review` | mark/unmark clinician-reviewed |
| GET | `/api/v1/fis/rules`, `/biomarkers` | introspect the live rule base / biomarker definitions |
| POST | `/api/v1/fis/simulate` | run the FIS on hypothetical values, no persistence |
| GET | `/api/v1/cohort/stats` | dashboard summary stats |
| GET | `/api/v1/health`, `/health/db` | liveness / DB connectivity |

Full request/response schemas: `/docs` (Swagger UI) or `/redoc`.

## Security notes

JWT auth (HS256, `PyJWT`), bcrypt password hashing, per-IP rate limiting (`slowapi`), CORS locked to
the configured frontend origin(s). `SECRET_KEY` **must** be overridden outside local dev — the default
is intentionally an obviously-fake placeholder that only works because `ENVIRONMENT=development`.
Uploaded scan files are never committed to the repo (`data/uploads/` is gitignored) and are stored
outside version control per the root README's [data-ethics note](../README.md#15-note-on-data-ethics).
