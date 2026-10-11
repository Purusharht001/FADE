"""Volumetry providers: what happens to a scan file once it is stored.

`VOLUMETRY_PROVIDER` selects one of three:

* `external` (default). The scan is stored and set to `awaiting_volumetry`.
  Someone runs FreeSurfer offline and attaches the stats through
  `POST /patients/{id}/scans/{scan_id}/stats`.
* `fastsurfer_docker`. FastSurfer runs in Docker as a background job. The
  scan moves `segmenting -> running_inference -> completed`, or to `failed`
  with the reason in `failure_reason`. The client polls
  `GET /patients/{id}/scans/{scan_id}`.
* `inhouse`. The phantom-tuned volumetry in `volumetry.py`, run inside the
  request as before. Allowed for PHANTOM patients only.

Background jobs are asyncio tasks in this process, each with its own
database session. There is no queue and no broker: a job that is running
when the server stops is lost, and `fail_interrupted_jobs` marks such
scans failed at the next startup so they do not sit in `segmenting`
forever. The stats-attachment endpoint can then finish them by hand.
"""

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, UnprocessableScanError
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.enums import DataSource, ExtractionMethod, ScanStatus
from app.models.patient import Patient
from app.models.scan import Scan
from app.repositories import patient_repo
from app.services import scan_service
from app.services.biomarker_derivation import derive_from_stats
from app.services.calibration import data_domain_for
from app.services.freesurfer_stats import StatsParseError, find_stats_files, parse_stats_dir
from app.services.pipeline import Provenance

logger = get_logger(__name__)

INHOUSE_NOT_FOR_REAL_DATA = (
    "in-house volumetry is validated only on the synthetic phantom; "
    "use external or fastsurfer_docker"
)

_FAILURE_REASON_MAX = 500
_LOG_TAIL_BYTES = 8192
_INTERRUPTED_REASON = (
    "Segmentation was interrupted by a server restart. Attach FreeSurfer stats to this scan, "
    "or upload it again."
)


class VolumetryProvider(StrEnum):
    EXTERNAL = "external"
    FASTSURFER_DOCKER = "fastsurfer_docker"
    INHOUSE = "inhouse"


class FastSurferError(Exception):
    """FastSurfer did not produce stats. The message is stored as the scan's
    `failure_reason`, so it must be safe to show a clinician.
    """


def active_provider(settings: Settings | None = None) -> VolumetryProvider:
    return VolumetryProvider((settings or get_settings()).volumetry_provider)


def check_provider_accepts(provider: VolumetryProvider, patient: Patient) -> None:
    """Refuses an upload the active provider must not process. Called before
    the file is read, so a refused upload is never written to disk.
    """
    is_phantom = patient.source == DataSource.PHANTOM
    if provider == VolumetryProvider.INHOUSE:
        if not is_phantom:
            raise UnprocessableScanError(INHOUSE_NOT_FOR_REAL_DATA)
        return
    if is_phantom:
        raise UnprocessableScanError(
            f"This patient is labelled PHANTOM, and the {provider.value} volumetry provider "
            "handles real scans only. Use a synthetic scan, or set VOLUMETRY_PROVIDER=inhouse "
            "to process an uploaded phantom volume."
        )


# --------------------------------------------------------------------------
# FastSurfer in Docker
# --------------------------------------------------------------------------


def fastsurfer_base_dir(settings: Settings) -> Path:
    return Path(settings.fastsurfer_output_dir or Path(settings.upload_dir) / "fastsurfer")


def docker_user(settings: Settings) -> str:
    if settings.fastsurfer_docker_user:
        return settings.fastsurfer_docker_user
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is not None and getgid is not None:
        return f"{getuid()}:{getgid()}"
    return "1000:1000"  # Windows: no POSIX ids, and Docker Desktop ignores ownership


def container_name(scan_id: uuid.UUID) -> str:
    return f"fade-fastsurfer-{scan_id.hex}"


def build_fastsurfer_command(
    nifti_path: Path, output_dir: Path, scan_id: uuid.UUID, settings: Settings
) -> list[str]:
    """The `docker run` argv for one scan.

    Only the one input file is mounted, read-only, so the container cannot
    see other patients' uploads. `--seg_only --no_hypothal --no_cc` runs the
    segmentation and its stats without the surface pipeline.

    TODO(confirm): in `--seg_only` mode FastSurfer may not write an eTIV
    measure into aseg+DKT.stats unless its Talairach registration runs. If
    jobs fail with "has no intracranial volume", add the flag your
    FastSurfer version needs (for example "--tal_reg") to
    FASTSURFER_EXTRA_ARGS. Not verified here: no Docker daemon or licence
    was available when this was written.
    """
    command = ["docker", "run", "--rm", "--name", container_name(scan_id)]
    if settings.fastsurfer_use_gpu:
        command += ["--gpus", "all"]
    command += [
        "-u",
        docker_user(settings),
        "-v",
        f"{nifti_path.resolve()}:/data/input.nii.gz:ro",
        "-v",
        f"{output_dir.resolve()}:/output",
    ]
    fastsurfer_args = ["--t1", "/data/input.nii.gz", "--sid", scan_id.hex, "--sd", "/output"]
    if settings.fs_license_path:
        command += ["-v", f"{Path(settings.fs_license_path).resolve()}:/fs_license/license.txt:ro"]
        fastsurfer_args += ["--fs_license", "/fs_license/license.txt"]
    fastsurfer_args += ["--seg_only", "--no_hypothal", "--no_cc", *settings.fastsurfer_extra_args]
    return [*command, settings.fastsurfer_image, *fastsurfer_args]


def read_log_tail(log_path: Path) -> str:
    """The last non-empty line of a FastSurfer log. Container output is not
    guaranteed to be valid UTF-8, hence errors="replace".
    """
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            text = handle.read().decode(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _stats_dir(output_dir: Path, scan_id: uuid.UUID) -> Path:
    return output_dir / scan_id.hex / "stats"


def _has_usable_stats(stats_dir: Path) -> bool:
    try:
        find_stats_files(stats_dir)
    except StatsParseError:
        return False
    return True


def run_fastsurfer(
    nifti_path: Path,
    scan_id: uuid.UUID,
    settings: Settings,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Runs FastSurfer for one scan and returns its `stats/` directory.
    Blocking: call it in a worker thread.

    Idempotent. If a previous run for this scan already left usable stats,
    they are returned without starting a container. Otherwise the scan's
    output directory is recreated empty first, since FastSurfer refuses to
    write into a half-finished subject directory.
    """
    base = fastsurfer_base_dir(settings)
    output_dir = base / scan_id.hex
    stats_dir = _stats_dir(output_dir, scan_id)
    if _has_usable_stats(stats_dir):
        return stats_dir

    if not nifti_path.is_file():
        raise FastSurferError("The stored scan file is missing; upload the scan again.")
    if settings.fs_license_path and not Path(settings.fs_license_path).is_file():
        raise FastSurferError("FS_LICENSE_PATH does not point to a FreeSurfer licence file.")
    if shutil.which("docker") is None:
        raise FastSurferError("Docker is not installed or not on PATH on the server.")

    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True)
    log_path = base / f"{scan_id.hex}.log"
    command = build_fastsurfer_command(nifti_path, output_dir, scan_id, settings)

    try:
        with log_path.open("wb") as log:
            completed = run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=settings.fastsurfer_timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        # Killing the docker CLI does not stop the container it started.
        subprocess.run(  # noqa: S603
            ["docker", "kill", container_name(scan_id)],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise FastSurferError(
            f"FastSurfer did not finish within {settings.fastsurfer_timeout_seconds} seconds."
        ) from exc
    except OSError as exc:
        raise FastSurferError("Docker could not be started on the server.") from exc

    if completed.returncode != 0:
        tail = read_log_tail(log_path)
        detail = f" Last log line: {tail}" if tail else ""
        raise FastSurferError(f"FastSurfer exited with status {completed.returncode}.{detail}")
    if not _has_usable_stats(stats_dir):
        raise FastSurferError("FastSurfer finished but wrote no aseg stats file.")
    return stats_dir


# --------------------------------------------------------------------------
# Background job
# --------------------------------------------------------------------------

# Keeps a reference to each running task (asyncio holds only weak ones) and
# makes launching the same scan twice a no-op.
_jobs: dict[uuid.UUID, asyncio.Task[None]] = {}


async def _mark_failed(db: AsyncSession, scan_id: uuid.UUID, reason: str) -> None:
    scan = await patient_repo.get_scan(db, scan_id)
    if scan is None:
        return
    scan.status = ScanStatus.FAILED
    scan.failure_reason = reason[:_FAILURE_REASON_MAX]
    await db.commit()


async def run_fastsurfer_job(
    scan_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    runner: Callable[[Path, uuid.UUID, Settings], Path] = run_fastsurfer,
) -> None:
    """Segments one scan and scores the result. Safe to call more than once:
    it does nothing unless the scan is in `segmenting`, and `run_fastsurfer`
    reuses stats an earlier attempt produced.
    """
    settings = get_settings()
    session_factory = session_factory or get_session_factory()

    async with session_factory() as db:
        scan = await patient_repo.get_scan(db, scan_id)
        if scan is None or scan.status != ScanStatus.SEGMENTING or scan.file_path is None:
            return
        patient = await patient_repo.get_patient_only(db, scan.patient_id)
        if patient is None:
            return
        nifti_path = Path(scan.file_path)
        domain = data_domain_for(patient.source)
        subject_id = scan.external_subject_id

        try:
            stats_dir = await asyncio.to_thread(runner, nifti_path, scan_id, settings)
            derived = derive_from_stats(parse_stats_dir(stats_dir))

            # Committed so a poller sees the segmentation has finished.
            scan.status = ScanStatus.RUNNING_INFERENCE
            await db.commit()

            await scan_service.process_derived_biomarkers(
                db,
                scan,
                derived,
                None,
                Provenance(
                    extraction_method=ExtractionMethod.FASTSURFER,
                    data_domain=domain,
                    external_subject_id=subject_id,
                ),
            )
            await db.commit()
            logger.info("fastsurfer_job_completed", scan_id=str(scan_id))
        except (FastSurferError, AppError) as exc:
            await db.rollback()
            reason = exc.message if isinstance(exc, AppError) else str(exc)
            await _mark_failed(db, scan_id, reason)
            logger.warning("fastsurfer_job_failed", scan_id=str(scan_id), reason=reason)
        except Exception:  # noqa: BLE001 - a background task must never die silently
            await db.rollback()
            await _mark_failed(db, scan_id, "Segmentation failed with an unexpected server error.")
            logger.exception("fastsurfer_job_crashed", scan_id=str(scan_id))


def launch_fastsurfer_job(scan_id: uuid.UUID) -> asyncio.Task[None]:
    """Starts the job for `scan_id` without blocking, or returns the one
    already running for it.
    """
    existing = _jobs.get(scan_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(run_fastsurfer_job(scan_id), name=f"fastsurfer-{scan_id.hex}")
    _jobs[scan_id] = task
    task.add_done_callback(lambda _finished: _jobs.pop(scan_id, None))
    return task


async def fail_interrupted_jobs(db: AsyncSession) -> int:
    """Marks scans left in `segmenting` by a previous process as failed.
    Run once at startup, when no job of this process can be running yet.
    """
    result = await db.execute(select(Scan).where(Scan.status == ScanStatus.SEGMENTING))
    stranded = [scan for scan in result.scalars().all() if scan.id not in _jobs]
    for scan in stranded:
        scan.status = ScanStatus.FAILED
        scan.failure_reason = _INTERRUPTED_REASON
    if stranded:
        await db.commit()
        logger.warning("fastsurfer_jobs_interrupted", count=len(stranded))
    return len(stranded)


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------


async def submit_stored_scan(
    db: AsyncSession,
    scan: Scan,
    nifti_path: Path,
    provider: VolumetryProvider,
    *,
    launch: Callable[[uuid.UUID], object] | None = None,
) -> Scan:
    """Hands a stored, sanitised NIfTI to the active provider. `launch`
    starts the background job; it defaults to `launch_fastsurfer_job`.
    """
    if provider == VolumetryProvider.INHOUSE:
        return await scan_service.process_uploaded_scan(db, scan, str(nifti_path))

    if provider == VolumetryProvider.EXTERNAL:
        scan.status = ScanStatus.AWAITING_VOLUMETRY
        await db.flush()
        return scan

    scan.status = ScanStatus.SEGMENTING
    scan.extraction_method = ExtractionMethod.FASTSURFER
    # Committed before the job starts: the job reads the scan through its
    # own session and must be able to see it.
    await db.commit()
    (launch or launch_fastsurfer_job)(scan.id)
    return scan
