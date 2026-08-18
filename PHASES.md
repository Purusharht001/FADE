# FADE — Project Phases

Detailed phase-by-phase plan. Phases 2–3 and 4 can run partly in parallel across the three-person
team (one on the imaging pipeline, one on the fuzzy system, one on validation/data-ethics tracking) —
noted per phase. Durations are rough planning estimates for a two-semester final-year project; adjust
against your actual academic calendar and Dr. Deshmukh's availability.

---

## Phase 0 — Foundation & Literature Review *(done)*

**Goal:** Establish feasibility, methodology, and the specific novelty gap FADE fills.

**Status:** Complete — 7 papers reviewed spanning fuzzy-only AD staging, CNN-fuzzy hybrids, and
multimodal fusion approaches. Identified gap: existing fuzzy AD-staging work is benchmark-only and not
built around clinical workflow.

**Deliverables:** Problem statement, reference list, project proposal (this document's source PDF).

---

## Phase 1 — Requirements, Ethics & Data Access

**Goal:** Unblock every downstream data dependency before technical work depends on it.

**Key tasks:**
- Register for OASIS access.
- Apply for ADNI access (this can take weeks — start immediately, don't wait on OASIS setup first).
- With Dr. Deshmukh: define the informed-consent process and anonymization protocol for clinic scans;
  confirm whether formal IRB/ethics-committee sign-off is required by your institution for real
  patient data, even anonymized.
- Define the exact biomarker/feature list clinically (confirm hippocampal volume, ventricle-to-brain
  ratio, cortical thickness are the right starting set — anything else Dr. Deshmukh considers
  diagnostically load-bearing?).
- Define the CN/MCI/AD staging criteria you'll treat as ground truth for benchmark datasets (which
  OASIS/ADNI clinical fields map to which stage).

**Deliverables:** Dataset access confirmations (or applications in flight), consent/anonymization
protocol document, finalized biomarker list, ground-truth labeling convention.

**Risks / dependencies:** This phase gates Phase 7 entirely and partially gates Phase 2. Run the
ethics/consent track in parallel with early Phase 2 technical work rather than sequentially — it is
usually the longest-lead-time item in the whole project.

---

## Phase 2 — Data Acquisition & Preprocessing Pipeline *(pipeline built; data acquisition blocked)*

**Goal:** A reproducible pipeline that takes a raw T1-weighted MRI and produces a preprocessed volume
ready for segmentation.

**Status:** The preprocessing pipeline itself is built and working —
[`backend/app/services/preprocessing.py`](backend/app/services/preprocessing.py): skull-stripping
(Otsu threshold + largest-connected-component + hole-filling), bias-field correction (normalized-
convolution homomorphic correction — an early version had a real edge-artifact bug, caught and fixed;
see `backend/tests/test_pipeline.py`'s regression test for it), and intensity normalization. What's
*not* done is Phase 1's half of this phase — OASIS/ADNI haven't been downloaded, so the pipeline runs
today against a synthetic MRI phantom
([`backend/app/services/synthetic_mri.py`](backend/app/services/synthetic_mri.py)) instead of real
scans. Swapping in real data means pointing `preprocess_file()` at real NIfTI files — the code doesn't
change.

**Key tasks:**
- Download/organize OASIS (and ADNI once access is granted). *(blocked on Phase 1)*
- ~~Build preprocessing pipeline: skull stripping, bias field correction, spatial normalization,
  intensity normalization~~ — done.
- Validate the pipeline runs cleanly across OASIS's scanner/protocol variety — this is your rehearsal
  for the heterogeneity real clinic scans will introduce later. *(pending real data)*
- ~~Version and document the pipeline~~ — done; see `backend/README.md`.

**Deliverables:** Preprocessing pipeline (done, scripted), preprocessed OASIS cohort (pending Phase 1),
pipeline documentation (done).

**Can run in parallel with:** Phase 4 (FIS design can start on synthetic/toy biomarker values before
real extracted values exist) — this is exactly what happened; both were built together.

---

## Phase 3 — Volumetric Feature Extraction *(built, against the synthetic phantom)*

**Goal:** Automated extraction of the three core biomarkers from preprocessed scans.

**Status:** [`backend/app/services/volumetry.py`](backend/app/services/volumetry.py) extracts all
three biomarkers, but *not* via FreeSurfer/FSL — no license/install for those in this environment, and
they take minutes-to-hours per scan besides. Instead: CSF/ventricle via a fixed intensity threshold,
cortex via a fixed intensity threshold, hippocampus via a spatial-prior ROI (a stand-in for an
atlas-registration prior) refined by a soft Gaussian intensity weight. Getting here took real
iteration — a first attempt using global multi-Otsu thresholding was tried and discarded because it
can't isolate a small, low-contrast structure like the hippocampus from its much larger neighbors
(documented in the module's docstring, with the reasoning kept rather than deleted). Validated for
monotonicity and plausible magnitude against the phantom's own ground truth across severities and
multiple random seeds — see `backend/tests/test_pipeline.py`.

**Key tasks:**
- Integrate a segmentation tool (e.g. FreeSurfer/FSL FIRST) or train/apply a segmentation model.
  *(deferred — see Status; real tool integration is the swap-in point once Phase 1 data exists)*
- ~~Extract: hippocampal volume, ventricle-to-brain ratio, cortical thickness~~ — done.
- Normalize volumetric measures against intracranial volume (head-size correction) — without this,
  biomarker values aren't comparable across subjects. *(not yet needed — single fixed phantom scale;
  revisit once real, variable-sized scans are in play)*
- Sanity-check extracted values against published normative ranges before trusting them as FIS input.
  *(the phantom's own achievable ranges required recalibrating the fuzzy breakpoints away from
  literal clinical numbers — see `backend/app/services/biomarkers.py`'s docstring for why)*

**Deliverables:** Feature-extraction module (done), extracted biomarker table for the full OASIS
cohort (pending Phase 1), normative-range sanity check report (done, against the phantom).

**Risks:** Segmentation tool choice affects both accuracy and runtime — benchmark 1-2 options early
rather than committing blind. *(Still applies once real FreeSurfer/FSL integration starts — the
current phantom-calibrated segmentation is explicitly not that benchmark.)*

---

## Phase 4 — Fuzzy Inference System Design *(engine built; clinician review still pending)*

**Goal:** A clinician-informed FIS that maps biomarker values to a dementia stage + confidence score.

**Status:** [`backend/app/services/fis_engine.py`](backend/app/services/fis_engine.py) is a real
multi-antecedent Mamdani engine — each biomarker fuzzified independently against trapezoidal
membership functions, 10 explicit rules combined with fuzzy AND (min) / OR (max), max-aggregation per
stage, normalized to a confidence distribution. A custom engine was used instead of `scikit-fuzzy`
(unmaintained, dependency-version friction with modern numpy) — full control over the rule DSL turned
out to matter more than the library would have saved. **What's not done:** the rule base and
membership-function breakpoints are the team's own literature-informed guesses, not yet reviewed with
Dr. Deshmukh — this is the one item in this phase that's still exactly as open as the phase originally
described, and is the highest-value next conversation with him.

**Key tasks:**
- ~~Define linguistic variables and membership functions per biomarker~~ — done, literature-informed.
- Design the rule base with Dr. Deshmukh directly — this is the step the proposal singles out as the
  key differentiator, treat it as a real working session, not a formality. **Still open.**
- ~~Choose inference approach (Mamdani for interpretability vs. Sugeno)~~ — done, Mamdani.
- ~~Design the confidence/uncertainty score~~ — done: confidence is the winning stage's normalized
  share; uncertainty is derived from its separation from the runner-up stage (a close CN/MCI call
  reads as high uncertainty — this is what the triage dashboard sorts by).
- ~~Implement in `scikit-fuzzy` or a custom engine~~ — done, custom engine (see Status above).

**Deliverables:** Rule base (documented in code with per-rule clinical rationale — done; *reviewed by
Dr. Deshmukh* — not done), working FIS module (done), confidence-score definition (done).

**Can run in parallel with:** Phase 2/3 (using literature-derived or synthetic biomarker values until
real extracted values are ready) — this is exactly what happened.

---

## Phase 5 — System Integration *(built)*

**Goal:** One pipeline: MRI in → preprocessing → volumetry → FIS → stage + confidence out.

**Status:** Done, and exposed as a real service, not just a script —
[`backend/app/services/pipeline.py`](backend/app/services/pipeline.py) orchestrates
preprocessing → volumetry → FIS, and [`backend/app/services/scan_service.py`](backend/app/services/scan_service.py)
ties that to persistence (SQLAlchemy models, a Postgres/SQLite-backed API). A clinician — or the
frontend, once wired up — hits `POST /api/v1/patients/{id}/scans/upload` (or `/scans/synthetic` for
the demo phantom) and gets back a persisted `Scan` with biomarkers, fuzzy membership, fired rules, and
a confidence/uncertainty score, via a documented REST API (`backend/README.md` has the full surface).

**Key tasks:**
- ~~Wire Phases 2–4 into a single callable pipeline~~ — done.
- ~~Add logging/traceability so a given output can be traced back to its input biomarker values~~ —
  done: structured logging (`structlog`) plus the persisted `BiomarkerReading` and `FISResult` rows
  each scan produces are the audit trail.
- ~~Basic error handling for malformed/low-quality scans~~ — done: a dedicated
  `UnprocessableScanError` (422) distinguishes "this scan needs manual handling" from a generic
  server error, at every pipeline stage (skull-strip failure, degenerate contrast, empty segmentation).

**Deliverables:** End-to-end pipeline (done), integration tests (done — `backend/tests/`, including
API-level tests against a real HTTP client, not just unit tests of the pipeline functions). Tests on a
handful of *known OASIS cases* specifically are still pending Phase 1.

---

## Phase 6 — Benchmark Validation

**Goal:** Quantify staging performance against public-dataset ground truth before trusting the system
on real patients.

**Key tasks:**
- Run the full pipeline on held-out OASIS (and ADNI, if available) subjects.
- Compare against ground-truth clinical labels: accuracy, per-class sensitivity/specificity
  (MCI will likely be the weakest class — expect and report this rather than treating it as a bug).
- Compare qualitatively against results reported in references [1], [3], [5] as a sanity check, not a
  competition — methodology and cohorts differ.

**Deliverables:** Benchmark results report, confusion matrix per dataset, comparison against reference
literature.

---

## Phase 7 — Real-World Clinical Validation

**Goal:** Test the generalization gap the proposal explicitly targets — the part prior work skipped.

**Key tasks:**
- Obtain anonymized, consented scans from Dr. Deshmukh's clinic (per Phase 1 protocol).
- Run the identical Phase 5 pipeline unmodified — if it needs clinic-specific tweaks, that's itself a
  finding about generalization, document it rather than silently patching it away.
- Compare FADE's stage + confidence output against Dr. Deshmukh's independent clinical assessment.
- Compute clinician-agreement metrics (e.g. Cohen's κ) and specifically check whether low-confidence
  outputs correlate with cases Dr. Deshmukh independently flags as borderline — this is the crux
  metric for the project's core "reduce workload" claim.

**Deliverables:** Real-world validation report, clinician-agreement analysis, qualitative feedback log
from Dr. Deshmukh.

**Hard dependency:** Phase 1 consent/ethics sign-off, Dr. Deshmukh's scheduling availability.

---

## Phase 8 — Decision-Support Interface *(built, wired to the real backend)*

**Goal:** Make the tool usable by a clinician, not just runnable by the team.

**Status:** A React/TypeScript dashboard is built in [`frontend/`](frontend/) and talks to
[`backend/`](backend/) over a real JWT-authenticated REST API — triage list sorted by diagnostic
uncertainty (server-computed, client re-applied as a defense-in-depth guarantee — see
`frontend/src/lib/sort.ts`), per-case biomarker gauges, per-biomarker fuzzy membership charts (plotting
points the API pre-samples — the frontend never runs `trapmf` or any fuzzy-logic computation itself),
and a rule-explainability panel. Login, case creation (upload a real `.nii`/`.nii.gz` or generate a
synthetic demo scan), and clinician review all round-trip through the live pipeline, including
graceful handling of a 422 `UnprocessableScanError` (inline retry in the "run scan" dialog; a toast +
navigate-to-record when the failure happens during case creation, so the clinician isn't stranded).
Covered by a vitest + React Testing Library suite. See [`frontend/README.md`](frontend/README.md) for
the API client / auth-store / React Query architecture.

**Key tasks:**
- ~~Build a minimal interface showing: input scan, extracted biomarkers, staged output, confidence
  score, and which rules fired and why~~ — done, against the real API.
- ~~Design the triage view: sort/flag cases by confidence~~ — done; sorted by uncertainty specifically
  (top-two stage membership separation), which is the actual triage signal.
- ~~Wire to the real backend~~ — done: `src/lib/mock-data.ts` and `src/lib/fis.ts` are deleted; every
  clinical number rendered comes from the API.
- Remaining: usability walkthrough with Dr. Deshmukh using real (or clinically realistic) cases instead
  of the synthetic phantom.

**Deliverables:** Working demo interface (done, live API), usability walkthrough with Dr. Deshmukh
(pending real/realistic data — the one item still blocked on Phase 1).

---

## Phase 9 — Evaluation, Iteration & Clinical Feedback Loop

**Goal:** Close the loop — feed Phase 7's real-world results and Dr. Deshmukh's feedback back into the
rule base and membership functions.

**Key tasks:**
- Review disagreements between FADE and clinician judgment with Dr. Deshmukh case by case.
- Refine membership functions / rules where systematic bias is found.
- Re-run Phase 6/7 validation after changes to confirm improvement (and check nothing regressed).

**Deliverables:** Revised rule base v2, before/after comparison, documented rationale for each change.

---

## Phase 10 — Documentation & Presentation

**Goal:** Package the work for academic submission/demo.

**Key tasks:**
- Final report: problem, method, architecture, results (benchmark + real-world), limitations, future
  work.
- Reproducibility: clean repo, README, environment/requirements file.
- Demo prep: live or recorded walkthrough of the pipeline and the interface, ideally including a
  segment with Dr. Deshmukh's clinical perspective on the tool's value.

**Deliverables:** Final report, presentation deck/demo, cleaned-up public-facing repository (with all
patient data excluded per §13 of the README).
