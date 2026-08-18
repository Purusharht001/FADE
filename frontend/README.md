# FADE Frontend — Triage Dashboard

Clinician-facing decision-support UI for [FADE](../README.md), implementing **Phase 8** of the
[project roadmap](../PHASES.md). Wired to the live [`backend/`](../backend/) API — every clinical
number it renders (stage, confidence, uncertainty, fired rules, fuzzy membership curves) is computed
server-side and fetched, never derived here. There is no client-side fuzzy logic anywhere in this
codebase.

## What it demonstrates

- **Auth** — JWT login/register against the real API, a route guard that redirects unauthenticated
  visitors to `/login`, session persisted in `localStorage`.
- **Triage dashboard** — cases ranked by diagnostic *uncertainty* (how close the top two fuzzy stage
  memberships are), not by scan date or patient ID. That ranking is the whole workload-reduction thesis
  of the project made concrete. Sorted server-side; re-applied client-side (`lib/sort.ts`) as a
  defense-in-depth guarantee so filtering/search can never silently reorder it.
- **Case detail view** — defuzzified stage + confidence, per-biomarker fuzzy membership charts (the API
  pre-samples the (x, degree) points; the frontend only plots them), per-biomarker gauges against
  clinical normal ranges, and a rule-explainability panel showing exactly which fuzzy rules fired and
  why.
- **New Case / Run Scan flows** — create a patient and either upload a real `.nii`/`.nii.gz` file or
  generate a synthetic demo scan (there's no real imaging dataset wired up yet — see the root README's
  Phase 1 status), with **explicit handling of a failed scan**: a 422 `UnprocessableScanError` from the
  backend surfaces inline in the dialog (stays open for retry) when re-running a scan, or as a toast +
  navigate-to-record when it happens during initial case creation, so the clinician always ends up
  somewhere useful instead of a dead end.
- **Live processing states** — a scan mid-pipeline polls and shows its real status
  (preprocessing/segmenting/inferring), not a fake spinner.
- **Light/dark themes**, both built from the same design-token set.

## Stack

Vite + React 19 + TypeScript, Tailwind CSS v4, Radix primitives (shadcn-style local components in
`src/components/ui`), TanStack Query for server state, Zustand for client state (auth session, theme,
toasts), Recharts for charts, Framer Motion for motion, React Router for navigation.

## Running

```bash
cp .env.example .env   # VITE_API_BASE_URL — defaults to http://localhost:8000
npm install
npm run dev      # dev server — needs backend/ running (see backend/README.md)
npm run build    # production build (type-checks first)
npm test         # vitest — sort logic, dashboard rendering/loading/error states, API error parsing
```

Demo login (seeded by `backend/scripts/seed.py`): `clinician@fade.demo` / `fade-demo-2026`.

## Structure

```
src/
├── api/                 # typed fetch client + React Query hooks, one file per backend resource
│   ├── client.ts          # fetch wrapper, ApiError (parses both backend error shapes), 401 handling
│   ├── auth.ts, patients.ts, fis.ts, cohort.ts
├── components/
│   ├── ui/                # local shadcn-style primitives (Button, Card, Badge, Dialog, Tabs, Toast, …)
│   ├── auth/               # RequireAuth route guard
│   ├── layout/             # app shell, sidebar, topbar
│   ├── dashboard/          # triage dashboard widgets, New Case dialog
│   └── patient/            # case-detail widgets (gauges, fuzzy membership chart, rule list, run-scan dialog)
├── lib/
│   ├── sort.ts             # sortPatientsByUncertainty — orders an already-computed number, computes nothing clinical
│   └── stage-style.ts      # CN/MCI/AD → color/label mapping (UI presentation only)
├── pages/                 # Login, Dashboard, PatientDetail (route-level code-split)
├── store/                 # auth session, theme, toast notifications (all Zustand)
└── types/api.ts           # hand-written mirror of backend/app/schemas/*.py — the wire contract
```

## Where the boundary is

`src/types/api.ts` documents this precisely, but the short version: this app fetches, renders, and lets
a clinician navigate/filter/sort — it never computes a stage, a confidence score, a fuzzy membership
degree, or an abnormality value. The one exception worth naming explicitly is
`lib/stage-style.ts#abnormalityLabel`, which buckets an *already-computed* backend abnormality score
into a UI badge color (normal/borderline/abnormal) — that's a presentation choice over a number the API
produced, not a re-derivation of it.

## Data note

All patient data in this repo comes from `backend/`'s synthetic MRI phantom (see the backend README) —
there is no real patient data anywhere in this project. See the root
[README's data-ethics note](../README.md#15-note-on-data-ethics).
