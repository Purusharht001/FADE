# FADE real-data runbook

How to get real data into the FADE backend, process it, and get results out.
It covers four jobs:

- [A. Import the OASIS cohort](#a-import-the-oasis-cohort)
- [B. Process a clinic scan](#b-process-a-clinic-scan), with each volumetry provider
- [C. Anonymise a scan before it leaves the clinic](#c-anonymise-a-scan-before-it-leaves-the-clinic)
- [D. Drop in a real calibration file](#d-drop-in-a-real-calibration-file) (for the ML teammate)

Two shorter sections follow: [E. The rating study and the export](#e-the-rating-study-and-the-export)
and [F. What has not been verified](#f-what-has-not-been-verified).

All commands are PowerShell, run from `backend\` unless stated otherwise. `curl.exe` is written
with its extension because plain `curl` is a different command in Windows PowerShell.

## Before you start

### One thing to know first

**Until a real calibration file is installed (section D), every real scan comes back
"indeterminate": no stage, flagged for review.** That is deliberate. The only calibration that
exists today was fitted to the synthetic phantom, and the engine refuses to apply it to real
measurements. You can import the cohort and upload clinic scans now; they are stored with their
biomarkers and will be staged when you re-score them after section D.

### Upgrade the database

The schema has changed. The server checks it at startup and refuses to start against a database
that is behind, naming this command:

```powershell
uv run alembic upgrade head
```

Run it once after pulling this branch. On an existing database it also corrects old rows:

- Every existing scan is marked `inhouse_phantom`, and every patient with a completed scan is
  relabelled `PHANTOM`. Until now only the phantom could produce a completed scan, but the seed
  script labelled those patients OASIS, ADNI or Clinic.
- Stored results in which no rule fired were recorded as CN. They become indeterminate.

A brand-new SQLite file needs no command: the server creates it on first start.

### Choose how requests are authenticated

| `AUTH_MODE` | Behaviour | Use it for |
|---|---|---|
| `demo` | A request with no token acts as one shared demo clinician. Default when `ENVIRONMENT` is not `production`. | Synthetic data on your own machine. |
| `required` | Every request needs a bearer token. Sign-up is closed. Default when `ENVIRONMENT=production`. | Anything with real patient data. |

`AUTH_MODE=demo` with `ENVIRONMENT=production` stops the server from starting. So does the
placeholder `SECRET_KEY`.

**The dashboard has no login screen yet**, so it only works in demo mode. Until one is built, do
real-data work through the API and the command-line tools in this runbook, with
`AUTH_MODE=required` in `backend\.env`.

### Create accounts

Sign-up through the API only creates clinicians, and is closed when auth is required. Create
accounts from the command line. It prompts for the password; do not put a password on the command
line.

```powershell
uv run python scripts/create_user.py --email ml@team.example --name "ML Teammate" --role researcher
uv run python scripts/create_user.py --email dr@clinic.example --name "Dr Example" --role clinician
```

| Role | Can |
|---|---|
| `clinician` | Use the triage endpoints, upload scans, rate study cases. |
| `researcher` | Use the triage endpoints and download the research export. Cannot rate. |
| `admin` | Same as researcher. |

To get a token for the examples below:

```powershell
$api = "http://127.0.0.1:8000/api/v1"
$body = @{ email = "ml@team.example"; password = "<your password>" } | ConvertTo-Json
$token = (Invoke-RestMethod -Method Post -Uri "$api/auth/login" -ContentType "application/json" -Body $body).accessToken
$auth = "Authorization: Bearer $token"
```

In demo mode you can leave the `-H $auth` argument out of the commands below.

---

## A. Import the OASIS cohort

`scripts/import_cohort.py` creates one `OASIS` patient and one scan per session. It takes one of
two inputs.

### Input 1: a biomarker CSV

```
subject_id,cdr,age,sex,icv_mm3,hipp_mm3,hipp_norm,vbr,cortex_gm_norm
FAKE_0001_MR1,0,74,F,1500000,8000,5.3333,0.0218,296.67
```

(An invented row, to show the format. `backend\tests\fixtures\cohort\cohort.csv` is a complete
invented example you can dry-run against.)

| Column | Meaning |
|---|---|
| `subject_id` | Research identifier. Letters, digits, `_`, `.`, `-` only. |
| `cdr` | Clinical Dementia Rating: 0, 0.5, 1, 2 or 3. Blank if not assessed. |
| `age`, `sex` | Sex is `M` or `F`. |
| `icv_mm3`, `hipp_mm3` | Intracranial volume and total (left + right) hippocampal volume. |
| `hipp_norm` | `hipp_mm3 / icv_mm3 x 1000`, in per-mille of ICV. |
| `vbr` | Ventricle-to-brain ratio as a ratio (`0.041`, not `4.1`). |
| `cortex_gm_norm` | Cortical grey matter over ICV, per-mille. Stored, not used by the engine. |
| `cortical_thickness_mm` | Optional. Mean cortical thickness in mm. |

Without a thickness column every row is missing one of the three engine inputs, so every result
stays indeterminate even after a real calibration is installed. Use input 2 if you have the
`aparc` stats.

### Input 2: FreeSurfer stats folders plus the demographics sheet

```
D:\oasis\subjects\
    OAS1_0001_MR1\stats\aseg.stats
    OAS1_0001_MR1\stats\lh.aparc.stats
    OAS1_0001_MR1\stats\rh.aparc.stats
    OAS1_0002_MR1\stats\...
D:\oasis\oasis_cross-sectional.xlsx      (columns ID, M/F, Age, CDR, ...)
```

The folder name is the subject id. FastSurfer file names (`aseg+DKT.stats`,
`lh.aparc.DKTatlas.mapped.stats`) are accepted too. The demographics file may be `.xlsx` or `.csv`.

### Before the first import: settle the VBR formula

The import computes and stores VBR. The formula is the team's best reading of the earlier parser
and is not yet confirmed (section D, "Good to know", has the one-command check against
OAS1_0001_MR1). Run that check first. A scan imported before a formula fix keeps the old value,
and there is no command yet that re-derives stored values from raw volumes.

### Steps

1. **Dry run.** Nothing is written; the summary is exactly what a real run would produce.

   ```powershell
   uv run python scripts/import_cohort.py --csv D:\oasis\biomarkers.csv --dry-run
   ```

   or

   ```powershell
   uv run python scripts/import_cohort.py --stats-dir D:\oasis\subjects --demographics D:\oasis\oasis_cross-sectional.xlsx --dry-run
   ```

2. **Read the summary.**

   ```
   Cohort import summary (DRY RUN: nothing was written)
     imported:          5
     skipped-no-label:  2
     skipped-MR2:       1
     skipped-existing:  0
     skipped-invalid:   1
     QC-flagged:        2
     indeterminate:     5 of 5 imported
     invalid  FAKE_0008_MR1: sex is not M or F
   ```

   (This is the output for the invented fixture cohort, shown for its format.)

   | Line | Meaning |
   |---|---|
   | `skipped-no-label` | The row has no CDR. It is not imported. |
   | `skipped-MR2` | A `_MR2` reliability rescan of a subject already in the cohort. |
   | `skipped-existing` | Already in the database under the same `subject_id`. |
   | `skipped-invalid` | Could not be read. The reason is listed below the counts. |
   | `QC-flagged` | Imported, but a value is outside plausible bounds. See below. |
   | `indeterminate` | Imported but not staged. Expected for all rows until section D. |

3. **Run it for real** by removing `--dry-run`.

4. **Check** with the export (section E), filtered to the cohort:

   ```powershell
   curl.exe -H $auth -o oasis.csv "$api/export/results.csv?source=OASIS"
   ```

### Rules the import follows

- **A missing CDR is never filled in.** A previous script used `fillna(0.0)` and labelled 15
  unassessed subjects healthy. This import skips such rows and counts them. Add `--require-label`
  if you want it to stop and import nothing when any row has no CDR.
- **`_MR2` sessions are skipped**, so a subject is not counted twice. `--include-mr2` imports them.
- **It is idempotent.** Running it again imports only subjects that are not there yet. To apply a
  new calibration to subjects already imported, re-score them (section D); do not re-import.
- **QC flags do not reject a row.** A value outside these bounds is flagged, and a flagged scan is
  never staged:

  | Value | Plausible range | Setting |
  |---|---|---|
  | Intracranial volume | 1.0e6 to 2.2e6 mm3 | `PLAUSIBILITY_ICV_MM3` |
  | Hippocampus, left + right | 2000 to 12000 mm3 | `PLAUSIBILITY_HIPPOCAMPUS_TOTAL_MM3` |
  | Mean cortical thickness | 1.0 to 5.0 mm | `PLAUSIBILITY_THICKNESS_MM` |

  A CSV row is also flagged if `hipp_norm` does not equal `hipp_mm3 / icv_mm3 x 1000`, which
  catches a column that is a fraction or a percentage.
- The CDR is stored as the scan's reference label. No triage endpoint returns it; only the
  research export does.

Other options: `--source ADNI` (default `OASIS`), `--extraction-method fastsurfer` for a stats
folder produced by FastSurfer, `--verbose` for application logs. Inside the Docker image, which
has no `scripts\` folder, the command is `python -m app.cli.import_cohort`.

---

## B. Process a clinic scan

### Statuses a scan moves through

| Status | Meaning |
|---|---|
| `awaiting_volumetry` | The file is stored. Waiting for someone to attach FreeSurfer stats. |
| `segmenting` | FastSurfer is running in the background. |
| `running_inference` | Stats are parsed; the engine is scoring. Brief. |
| `completed` | Biomarkers and a result are stored. The result may still be indeterminate. |
| `failed` | Something went wrong. `failureReason` says what. |

Check a scan at any time:

```powershell
curl.exe -H $auth "$api/patients/<patient-id>/scans/<scan-id>"
```

### Common first steps

1. **Anonymise the scan** (section C). Do this at the clinic.

2. **Create the patient** with source `Clinic`. Age and sex only; there is no name field.

   ```powershell
   $patient = Invoke-RestMethod -Method Post -Uri "$api/patients" -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{"age": 71, "sex": "F", "source": "Clinic"}'
   $patientId = $patient.id
   ```

3. **Upload the scan.** NIfTI:

   ```powershell
   curl.exe -H $auth -F "file=@D:\scans\clinic-0007.nii.gz" "$api/patients/$patientId/scans/upload?externalSubjectId=CLINIC-0007"
   ```

   or a zip of the DICOM series:

   ```powershell
   curl.exe -H $auth -F "file=@D:\scans\clinic-0007.zip" "$api/patients/$patientId/scans/upload-dicom?externalSubjectId=CLINIC-0007"
   ```

   The response is the scan. Note its `id`. What happens next depends on the provider.

What the server does on the way in, whichever provider is active:

- Decides what the file is from its content, not its name. Rejects anything that is not NIfTI
  (or, on the DICOM endpoint, a zip of DICOM).
- Enforces `MAX_UPLOAD_MB` (64 by default; a zipped DICOM series can be larger, so raise it if
  uploads are refused).
- Stores the file under a random name. Your filename is not kept.
- Removes a trailing fourth dimension of size 1, and rejects anything that is not then a single
  3-D volume or whose voxel size is not between 0.1 and 10 mm.
- Rebuilds the NIfTI header, so the description fields and any embedded extension are blank.
- For DICOM: checks the identifying tags (section C), converts the series to NIfTI, and deletes
  the DICOM, whether or not the upload succeeded.

`externalSubjectId` is optional. Use a research code such as `CLINIC-0007`. A value containing a
space or other punctuation is rejected, because that is what a name or a hospital number looks
like; choosing a code that identifies nobody is still your responsibility.

### Provider 1: `external` (the default)

You run FreeSurfer or FastSurfer yourself and attach its stats.

1. Upload as above. The scan comes back `awaiting_volumetry`.
2. Run FreeSurfer (`recon-all`) or FastSurfer on your own copy of the same anonymised file.
3. Attach the stats:

   ```powershell
   curl.exe -H $auth -F "aseg=@D:\fs\clinic-0007\stats\aseg.stats" -F "lh_aparc=@D:\fs\clinic-0007\stats\lh.aparc.stats" -F "rh_aparc=@D:\fs\clinic-0007\stats\rh.aparc.stats" "$api/patients/$patientId/scans/<scan-id>/stats"
   ```

   Add `-F "extraction_method=fastsurfer"` if FastSurfer produced them.
4. The scan comes back `completed`.

Notes:

- `aseg` is required. `lh_aparc` and `rh_aparc` are optional but must be given together. Without
  them cortical thickness is not measured and the result is indeterminate.
- **FastSurfer on Windows:** `aseg+DKT.stats` is a Linux symbolic link and reads as an empty file.
  Upload `aseg+DKT.VINN.stats` instead. The server tells you this if you upload the empty one.
- A stats file that cannot be parsed is rejected with the reason (a missing structure, no
  intracranial volume, a line that is not numeric) and the scan stays `awaiting_volumetry`, so
  you can attach a corrected file.

If you already have the stats and no need to store the image, skip the upload and create the scan
directly:

```powershell
curl.exe -H $auth -F "aseg=@aseg.stats" -F "lh_aparc=@lh.aparc.stats" -F "rh_aparc=@rh.aparc.stats" -F "external_subject_id=CLINIC-0007" "$api/patients/$patientId/scans/freesurfer"
```

Or post numbers as JSON to `/patients/{id}/scans/biomarkers`, either raw volumes in mm3 or the
three derived values with units. Units are checked, not converted: hippocampal volume must be
`‰ICV` (or `permille_ICV`), VBR `ratio`, thickness `mm`.

### Provider 2: `fastsurfer_docker`

The server runs FastSurfer for you in Docker, in the background.

One-time setup:

1. Start Docker Desktop and pull the image: `docker pull deepmi/fastsurfer:latest`.
2. Put your FreeSurfer licence file somewhere on this machine.
3. In `backend\.env`:

   ```
   VOLUMETRY_PROVIDER=fastsurfer_docker
   FS_LICENSE_PATH=C:/freesurfer/license.txt
   FASTSURFER_USE_GPU=false
   ```

   Set `FASTSURFER_USE_GPU=true` only if Docker can see an NVIDIA GPU. Without a GPU the
   segmentation is much slower; raise `FASTSURFER_TIMEOUT_SECONDS` (3 hours by default) if jobs
   time out.
4. Run the API directly on this machine (`uv run uvicorn app.main:app`). This provider starts
   `docker run` itself, so it does not work from inside the `docker compose` API container.

Per scan:

1. Upload as above. The request returns at once with the scan in `segmenting`.
2. Poll the scan until it is `completed` or `failed`.
3. If it failed, read `failureReason`. The container's full output is in
   `<UPLOAD_DIR>\fastsurfer\<scan-id>.log`. You can still finish the scan by attaching stats by
   hand, exactly as in provider 1.

The command the server runs is:

```
docker run --rm --name fade-fastsurfer-<scan> [--gpus all] -u <uid>:<gid>
    -v <stored scan>:/data/input.nii.gz:ro  -v <output dir>:/output
    -v <licence>:/fs_license/license.txt:ro
    deepmi/fastsurfer:latest
    --t1 /data/input.nii.gz --sid <scan> --sd /output --fs_license /fs_license/license.txt
    --seg_only --no_hypothal --no_cc
```

Things to know:

- `--seg_only` produces no surfaces, so there are no `aparc` stats and **no cortical thickness**.
  Scans processed this way stay indeterminate (missing biomarker) even with a real calibration.
  For a staged result, use provider 1 with a full FreeSurfer or FastSurfer run.
- If jobs fail with "has no intracranial volume", your FastSurfer version does not write eTIV in
  this mode. Add the flag it needs to `FASTSURFER_EXTRA_ARGS`, for example
  `FASTSURFER_EXTRA_ARGS=["--tal_reg"]`. This has not been confirmed against a real run
  (section F).
- A job is lost if the server restarts while it runs. At the next start such scans are marked
  `failed` with a reason saying so; attach stats by hand or upload again.
- Running the same scan twice is safe. If an earlier run already left stats, they are reused.

### Provider 3: `inhouse`

The original phantom-tuned volumetry. Set `VOLUMETRY_PROVIDER=inhouse` to process uploaded
phantom volumes inside the request, as before.

It accepts `PHANTOM` patients only. For any other patient the upload is refused:

> in-house volumetry is validated only on the synthetic phantom; use external or fastsurfer_docker

The synthetic-scan endpoint (`/scans/synthetic`) does not depend on this setting and always works.

### Phantom and real data are kept apart

A patient holds either phantom data or real data, never both.

- A synthetic scan on a patient created as `Clinic` relabels that patient `PHANTOM`.
- A synthetic scan is refused on a patient that already has real data.
- Real data (stats, biomarkers, a clinic upload) is refused on a `PHANTOM` patient.

---

## C. Anonymise a scan before it leaves the clinic

The server is a second check, not the anonymisation step. Do the following at the clinic, before
a file is copied anywhere.

### Required

1. **Blank these DICOM header tags.** The server rejects a DICOM upload if any has a value:

   `PatientName`, `PatientID`, `PatientBirthDate`, `OtherPatientIDs`, `InstitutionName`,
   `ReferringPhysicianName`, `AccessionNumber`, `StudyDate`

   Most scanner consoles and PACS viewers have an "export anonymised" option; a DICOM anonymiser
   tool does the same. Replacing a name with a code is not enough for the server: the tag must be
   empty. A rejection lists which tags still have values, and never the values themselves.

2. **Rename files and folders.** Export folders are often named after the patient. Zip entry
   names are discarded by the server, but they are still in the zip you are carrying.

3. **Give the scan a research code** (`CLINIC-0007`) and keep the list linking codes to patients
   at the clinic, under the clinician's control. It must not be stored with the scans or sent to
   the team.

4. **Do not type identifying details into free-text fields.** The rating notes field is the one
   place the server cannot check.

### Things the server does not do

- **It does not remove the face.** A T1 head scan can be rendered into a recognisable face.
  Whether scans must be defaced before leaving the clinic is a question for the consent and
  ethics protocol agreed with Dr. Deshmukh; if they must, do it before upload.
- It does not inspect private or vendor-specific DICOM tags, or text burned into the image.
- It cannot tell whether a research code secretly identifies someone.

### What the server does enforce

- The eight-tag gate above, before any conversion. `REQUIRE_ANONYMISED_DICOM=true` is the
  default; leave it on.
- The original DICOM is deleted after conversion, on success and on failure.
- The stored NIfTI header is rebuilt from the image geometry, so nothing a converter wrote into
  it survives.
- Files are stored under random names. Error messages and logs never contain file contents or
  header values.
- The audit log accepts only numbers and short codes, so it cannot hold a name or a note.

Converting to NIfTI yourself with `dcm2niix` does not anonymise anything by itself, but the
server's header rebuild covers what `dcm2niix` writes into the NIfTI header. Do not upload its
`.json` sidecar anywhere; it repeats DICOM header fields.

---

## D. Drop in a real calibration file

For the ML teammate. A calibration file holds every number the engine uses: the fuzzy-set
breakpoints, `normal_range` and `display_range` for each biomarker, and the output sets used for
`severity`. Nothing else in the code needs to change.

### What the engine does until then

The default file, `phantom.v0.json`, is marked as fitted to phantom data. Every result records
why it was not staged, in `indeterminateReason`:

| Reason | Meaning |
|---|---|
| `calibration_not_validated_for_real_data` | Real data, but the active calibration is the phantom one. |
| `calibration_incomplete` | The active calibration file still has `null` values. |
| `calibration_domain_mismatch` | Phantom data, but the active calibration is a real one. |
| `unit_mismatch:<biomarkers>` | The value's unit is not the unit the calibration declares. |
| `missing_biomarker:<biomarkers>` | One of the three inputs was not measured. |
| `qc_flag:<codes>` | A value is outside plausible bounds. |
| `no_rule_fired` | The values fall outside every fuzzy set. |

Several reasons can apply at once; they are joined with `; `. An indeterminate result has
`stage: null`, `needsReview: true`, `confidence: 0`, `uncertainty: 100`, `severity: null`, and its
biomarkers have `abnormality: null`.

### Steps

1. **Copy the template** and give the copy a version of its own.

   ```powershell
   Copy-Item app\calibration\real.template.json app\calibration\real.v1.json
   ```

2. **Fill it in.** Replace every `null`. Set `"version"` (for example `"real.v1"`: letters,
   digits, `_ . : -` only). Leave `"domain": "real"`.

   ```
   "hippocampal_volume": {
     "unit": "‰ICV",
     "normal_range": [low, high],
     "display_range": [low, high],
     "sets": {
       "low":        [a, b, c, d],
       "borderline": [a, b, c, d],
       "normal":     [a, b, c, d]
     }
   }
   ```

   (The letters stand for your numbers.)

   - Each set is a trapezoid `[a, b, c, d]` with `a <= b <= c <= d`: zero below `a`, one from `b`
     to `c`, zero above `d`. Use `a == b` or `c == d` for a shoulder that stays at one.
   - Hippocampal volume and cortical thickness have sets `low`, `borderline`, `normal`. VBR has
     `normal`, `borderline`, `high`.
   - **Units are fixed** by the derivation code and checked on every scan: hippocampal volume in
     `‰ICV` ((left + right) / ICV x 1000), VBR as a `ratio` (0.04, not 4), thickness in `mm`.
     Write the breakpoints in those units. Do not change the `unit` strings.
   - `output_sets` are three trapezoids for CN, MCI and AD on the 0 to 1 severity axis. You may
     leave them `null`: scans are then staged without a `severity`.
   - `"provisional": true` is a label for readers. It does not change behaviour.

3. **Select it** in `backend\.env`, then restart the server:

   ```
   CALIBRATION_FILE=real.v1.json
   ```

   A bare name is looked up in `app\calibration\`; a full path also works. The server validates
   the file at startup. If it is malformed (unordered breakpoints, a missing set, a bad version
   label) the server stops and says which field. If it still contains `null`s it starts, logs a
   warning, and treats every scan as indeterminate.

4. **Re-score what is already stored.** Results keep the calibration they were computed with
   until you do this.

   ```powershell
   uv run python scripts/rescore.py --dry-run
   uv run python scripts/rescore.py
   ```

   ```
   Re-scored with calibration phantom.v0 (DRY RUN: nothing was written)
     scans:               5
     staged before:       0
     staged after:        0
     stage changed:       0
     still indeterminate: 5
           5  calibration_not_validated_for_real_data
           5  missing_biomarker
           2  qc_flag
           5  unit_mismatch
   ```

   (Output for the invented fixture cohort under the default phantom calibration, shown for its
   format. The lines under "still indeterminate" count scans per reason; a scan can have several.)
   `--source OASIS` limits it to one source. Re-scoring changes no
   measurement: it re-runs the engine on the stored biomarkers. A clinician's review tick on a
   replaced result is cleared.

5. **Export and evaluate** (section E). Every row carries `calibration_version`, so results
   from different calibration files can be told apart.

### Good to know

- **One calibration is active at a time.** Once a real one is active, phantom scans (the demo
  cohort, new synthetic scans) come back indeterminate with `calibration_domain_mismatch`.
- Changing rules or rule weights is a code change in `app/services/fis_engine.py`, not a
  calibration change. Bump `ENGINE_VERSION` there when you do, so results can be traced.
- `compute_vbr` in `app/services/biomarker_derivation.py` is the single definition of VBR:
  (left + right lateral ventricles + left + right inferior lateral ventricles) / BrainSegVol. It
  must reproduce 0.04143 for OAS1_0001_MR1. To check, put that subject's `aseg.stats` at
  `tests\fixtures\private\OAS1_0001_MR1\aseg.stats` (the folder is git-ignored) and run:

  ```powershell
  uv run pytest tests/test_biomarker_derivation.py -k oas1_0001 -v
  ```

  The test is skipped until that file is present. If it fails, the earlier parser used a
  different denominator or set of ventricles: fix `compute_vbr`. Scans already imported keep the
  VBR computed with the old formula. Nothing re-derives stored values yet, so those rows would
  have to be removed from the database and imported again, which is why this check comes before
  the first import.
- Cortical thickness is the plain mean of `ThickAvg` over entorhinal, parahippocampal,
  superiortemporal, middletemporal and inferiortemporal, both hemispheres. Change the regions
  with `CORTICAL_THICKNESS_REGIONS`, or weight by surface area with
  `CORTICAL_THICKNESS_WEIGHTING=surface_area`. Fit the calibration to whichever you choose.

---

## E. The rating study and the export

### Blind ratings

A clinician rates cases without seeing the engine's answer.

```powershell
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Uri "$api/study/cases" -Headers $headers

$rating = @{ stageCalled = "MCI"; borderline = $true; confidence = 3 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$api/study/cases/<scan-id>/rating" -Headers $headers -ContentType "application/json" -Body $rating
```

- A case shows the biomarkers, age and sex. It does not show the stage, confidence, severity,
  reference label, subject id or data source.
- Each rater sees the cases in their own fixed order.
- One rating per rater per scan. Posting again replaces it; the change is recorded in the audit
  log with the previous call.
- `confidence` is 1 (guessing) to 5 (certain). `notes` is optional free text.
- Phantom cases are left out unless you add `?includePhantom=true`.

**Blinding has a limit you must manage.** These endpoints hide the engine's output, but the same
account can open the triage endpoints and see it. For a real study give each rater their own
account, run the server with `AUTH_MODE=required`, and have raters use only the study screens.
In demo mode every anonymous rater is the same user and overwrites the same rating.

### Export

```powershell
curl.exe -H $auth -o results.csv "$api/export/results.csv"
```

Researchers and admins only. One row per scan:

| Columns | Content |
|---|---|
| `scan_id`, `external_subject_id`, `source`, `extraction_method`, `status`, `scan_date`, `age`, `sex` | Provenance. |
| `hippocampal_volume`, `ventricle_brain_ratio`, `cortical_thickness`, each with `_unit` | The engine's inputs. Empty if not measured. |
| `raw_*` | Raw volumes as parsed, for example `raw_icv_mm3`, `raw_thickness_mm.lh.entorhinal`. |
| `qc_flags` | Flag codes, separated by `;`. |
| `determinate`, `indeterminate_reason`, `stage`, `severity`, `confidence`, `uncertainty`, `needs_review` | The engine's output. |
| `calibration_version`, `engine_version` | What produced it. |
| `reference_label` | The CDR. Empty if none was recorded. |
| `n_ratings`, `rating_*` | The number of clinician ratings and the most recent one. |

This is the only endpoint that returns the reference label. Each download is recorded in the
audit log. Empty cells mean "no value"; nothing is filled with zero.

---

## F. What has not been verified

These parts are covered by automated tests using stand-ins, and have not been run for real:

| Not yet verified | Why | What to do |
|---|---|---|
| FastSurfer in Docker, end to end | No Docker daemon or FreeSurfer licence was available while this was built. The job, its status changes and its error handling are tested with a fake container. | Run one scan through provider 2 before relying on it. |
| Whether FastSurfer `--seg_only` writes eTIV, and whether it needs the licence | Could not be checked without a real run. | See the note under provider 2. |
| `dcm2niix` conversion | Not installed here. The command line is tested with a fake; the built-in converter is tested on synthetic DICOM. | Install `dcm2niix` on the server and upload one real anonymised series. |
| Real DICOM from the clinic's scanner | Only synthetic single-frame series were available. | If a series is refused as unconvertible, convert it to NIfTI with `dcm2niix` and upload that. |
| Migrations on PostgreSQL | Run here on SQLite only. The generated PostgreSQL statements were inspected by hand. | The CI job applies them to PostgreSQL; check that it passes. |
| VBR against the team's value 0.04143 | The OAS1_0001_MR1 stats file was not available. | Section D, "Good to know". |
| Real FreeSurfer stats files | The parser is tested on hand-written files in FreeSurfer's format. | Dry-run the cohort import and read the `skipped-invalid` reasons. |
