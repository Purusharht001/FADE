"""Phase 3: volumetry providers and the FastSurfer background job.

No Docker daemon is needed: `run_fastsurfer` takes the function it uses to
run a command, and the job takes the runner it calls, so both are driven
here with fakes that behave like a container would.
"""

import asyncio
import datetime
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import UnprocessableScanError
from app.models.enums import DataSource, ExtractionMethod, ScanStatus, Sex
from app.models.patient import Patient
from app.repositories import patient_repo
from app.services import volumetry_providers as vp
from app.services.volumetry_providers import (
    FastSurferError,
    VolumetryProvider,
    build_fastsurfer_command,
    check_provider_accepts,
    container_name,
    docker_user,
    fail_interrupted_jobs,
    launch_fastsurfer_job,
    read_log_tail,
    run_fastsurfer,
    run_fastsurfer_job,
    submit_stored_scan,
)

FIXTURES = Path(__file__).parent / "fixtures"
FREESURFER = FIXTURES / "freesurfer"
FASTSURFER_STATS = FIXTURES / "fastsurfer" / "stats"

SCAN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(_env_file=None, upload_dir=str(tmp_path / "uploads"), **overrides)


def _patient(source: DataSource) -> Patient:
    return Patient(display_id="PT-1", age=70, sex=Sex.F, source=source, scans=[])


def _nifti(tmp_path: Path) -> Path:
    path = tmp_path / "uploads" / "scan.nii.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not read by these tests")
    return path


def _host_output_dir(command: list[str]) -> Path:
    """The host directory a docker command mounts at /output."""
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]
    (output_mount,) = [m for m in mounts if m.endswith(":/output")]
    return Path(output_mount[: -len(":/output")])


def _fake_docker(*, stats_from: Path | None = FASTSURFER_STATS, returncode=0, log=b"", seen=None):
    """Stands in for `subprocess.run(["docker", "run", ...])`."""

    def run(command, *, stdout, stderr, timeout, check):
        output_dir = _host_output_dir(command)
        if seen is not None:
            seen.append(
                {
                    "command": command,
                    "timeout": timeout,
                    "output_dir_existed": output_dir.is_dir(),
                    "output_dir_contents": sorted(p.name for p in output_dir.iterdir()),
                }
            )
        stdout.write(log)
        if stats_from is not None:
            sid = command[command.index("--sid") + 1]
            shutil.copytree(stats_from, output_dir / sid / "stats")
        return subprocess.CompletedProcess(command, returncode)

    return run


@pytest.fixture(autouse=True)
def _docker_on_path(monkeypatch):
    monkeypatch.setattr(vp.shutil, "which", lambda name: f"/usr/bin/{name}")


class TestProviderSelection:
    def test_default_provider_is_external(self):
        assert Settings(_env_file=None).volumetry_provider == "external"
        assert vp.active_provider(Settings(_env_file=None)) == VolumetryProvider.EXTERNAL

    @pytest.mark.parametrize("source", [DataSource.CLINIC, DataSource.OASIS, DataSource.ADNI])
    def test_inhouse_is_refused_for_every_real_source(self, source):
        with pytest.raises(UnprocessableScanError) as excinfo:
            check_provider_accepts(VolumetryProvider.INHOUSE, _patient(source))
        assert excinfo.value.status_code == 422
        assert excinfo.value.message == (
            "in-house volumetry is validated only on the synthetic phantom; "
            "use external or fastsurfer_docker"
        )

    def test_inhouse_is_allowed_for_phantom(self):
        check_provider_accepts(VolumetryProvider.INHOUSE, _patient(DataSource.PHANTOM))

    @pytest.mark.parametrize(
        "provider", [VolumetryProvider.EXTERNAL, VolumetryProvider.FASTSURFER_DOCKER]
    )
    def test_real_providers_refuse_a_phantom_patient(self, provider):
        with pytest.raises(UnprocessableScanError, match="PHANTOM"):
            check_provider_accepts(provider, _patient(DataSource.PHANTOM))
        check_provider_accepts(provider, _patient(DataSource.CLINIC))


class TestFastSurferCommand:
    def _command(self, tmp_path, **overrides) -> list[str]:
        return build_fastsurfer_command(
            _nifti(tmp_path), tmp_path / "out", SCAN_ID, _settings(tmp_path, **overrides)
        )

    def test_segmentation_only_flags(self, tmp_path):
        command = self._command(tmp_path)
        image_at = command.index("deepmi/fastsurfer:latest")
        fastsurfer_args = command[image_at + 1 :]
        for flag in ("--seg_only", "--no_hypothal", "--no_cc"):
            assert flag in fastsurfer_args
        assert fastsurfer_args[fastsurfer_args.index("--t1") + 1] == "/data/input.nii.gz"
        assert fastsurfer_args[fastsurfer_args.index("--sd") + 1] == "/output"
        assert fastsurfer_args[fastsurfer_args.index("--sid") + 1] == SCAN_ID.hex

    def test_docker_options_come_before_the_image(self, tmp_path):
        command = self._command(tmp_path)
        assert command[:3] == ["docker", "run", "--rm"]
        image_at = command.index("deepmi/fastsurfer:latest")
        assert command.index("-u") < image_at
        assert command[command.index("--name") + 1] == container_name(SCAN_ID)

    def test_gpu_flag_only_when_enabled(self, tmp_path):
        assert "--gpus" not in self._command(tmp_path)
        gpu = self._command(tmp_path, fastsurfer_use_gpu=True)
        assert gpu[gpu.index("--gpus") + 1] == "all"
        assert gpu.index("--gpus") < gpu.index("deepmi/fastsurfer:latest")

    def test_user_is_passed(self, tmp_path):
        command = self._command(tmp_path, fastsurfer_docker_user="1234:5678")
        assert command[command.index("-u") + 1] == "1234:5678"

    def test_default_user_is_uid_gid_shaped(self, tmp_path):
        uid, gid = docker_user(_settings(tmp_path)).split(":")
        assert uid.isdigit() and gid.isdigit()

    def test_licence_is_mounted_from_the_setting(self, tmp_path):
        licence = tmp_path / "license.txt"
        licence.write_text("licence", encoding="utf-8")
        command = self._command(tmp_path, fs_license_path=str(licence))
        mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]
        assert f"{licence.resolve()}:/fs_license/license.txt:ro" in mounts
        assert command[command.index("--fs_license") + 1] == "/fs_license/license.txt"

    def test_no_licence_mount_without_the_setting(self, tmp_path):
        command = self._command(tmp_path)
        assert "--fs_license" not in command
        assert not any("fs_license" in arg for arg in command)

    def test_only_the_one_scan_file_is_mounted_read_only(self, tmp_path):
        nifti = _nifti(tmp_path)
        command = self._command(tmp_path)
        mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]
        assert f"{nifti.resolve()}:/data/input.nii.gz:ro" in mounts
        # The uploads directory itself, with other patients' scans, is not.
        assert not any(m.startswith(f"{nifti.parent.resolve()}:") for m in mounts)

    def test_image_and_extra_args_are_configurable(self, tmp_path):
        command = self._command(
            tmp_path, fastsurfer_image="deepmi/fastsurfer:v2.4", fastsurfer_extra_args=["--tal_reg"]
        )
        assert "deepmi/fastsurfer:v2.4" in command
        assert command[-1] == "--tal_reg"


class TestRunFastSurfer:
    def test_success_returns_the_stats_directory(self, tmp_path):
        settings = _settings(tmp_path)
        stats_dir = run_fastsurfer(_nifti(tmp_path), SCAN_ID, settings, run=_fake_docker())
        assert stats_dir == vp.fastsurfer_base_dir(settings) / SCAN_ID.hex / SCAN_ID.hex / "stats"
        assert (stats_dir / "aseg+DKT.VINN.stats").is_file()

    def test_output_directory_is_pre_created_and_empty(self, tmp_path):
        seen = []
        run_fastsurfer(_nifti(tmp_path), SCAN_ID, _settings(tmp_path), run=_fake_docker(seen=seen))
        assert seen[0]["output_dir_existed"] is True
        assert seen[0]["output_dir_contents"] == []

    def test_a_stale_partial_output_directory_is_emptied_first(self, tmp_path):
        settings = _settings(tmp_path)
        stale = vp.fastsurfer_base_dir(settings) / SCAN_ID.hex / SCAN_ID.hex / "mri"
        stale.mkdir(parents=True)
        (stale / "half-written.mgz").write_bytes(b"x")
        seen = []

        run_fastsurfer(_nifti(tmp_path), SCAN_ID, settings, run=_fake_docker(seen=seen))
        assert seen[0]["output_dir_contents"] == []

    def test_timeout_comes_from_settings(self, tmp_path):
        seen = []
        settings = _settings(tmp_path, fastsurfer_timeout_seconds=123)
        run_fastsurfer(_nifti(tmp_path), SCAN_ID, settings, run=_fake_docker(seen=seen))
        assert seen[0]["timeout"] == 123

    def test_rerun_reuses_existing_stats_without_starting_a_container(self, tmp_path):
        settings = _settings(tmp_path)
        nifti = _nifti(tmp_path)
        first = run_fastsurfer(nifti, SCAN_ID, settings, run=_fake_docker())

        def must_not_run(*args, **kwargs):
            raise AssertionError("a second container was started")

        assert run_fastsurfer(nifti, SCAN_ID, settings, run=must_not_run) == first

    def test_non_zero_exit_reports_the_last_log_line(self, tmp_path):
        log = b"Loading model\nERROR: could not allocate memory\n\n"
        with pytest.raises(FastSurferError) as excinfo:
            run_fastsurfer(
                _nifti(tmp_path),
                SCAN_ID,
                _settings(tmp_path),
                run=_fake_docker(stats_from=None, returncode=137, log=log),
            )
        assert str(excinfo.value) == (
            "FastSurfer exited with status 137. Last log line: ERROR: could not allocate memory"
        )

    def test_log_that_is_not_utf8_does_not_break_error_reporting(self, tmp_path):
        log = b"progress \xff\xfe\x9c bar\nERROR: seg\xe9mentation failed \xf0\x28\x8c\n"
        with pytest.raises(FastSurferError) as excinfo:
            run_fastsurfer(
                _nifti(tmp_path),
                SCAN_ID,
                _settings(tmp_path),
                run=_fake_docker(stats_from=None, returncode=1, log=log),
            )
        assert "ERROR: seg" in str(excinfo.value)
        assert "�" in str(excinfo.value)  # replaced, not raised

    def test_success_exit_without_stats_is_a_failure(self, tmp_path):
        with pytest.raises(FastSurferError, match="wrote no aseg stats file"):
            run_fastsurfer(
                _nifti(tmp_path), SCAN_ID, _settings(tmp_path), run=_fake_docker(stats_from=None)
            )

    def test_timeout_kills_the_container(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr(vp.subprocess, "run", lambda command, **kwargs: killed.append(command))

        def hang(command, *, timeout, **kwargs):
            raise subprocess.TimeoutExpired(command, timeout)

        settings = _settings(tmp_path, fastsurfer_timeout_seconds=5)
        with pytest.raises(FastSurferError, match="did not finish within 5 seconds"):
            run_fastsurfer(_nifti(tmp_path), SCAN_ID, settings, run=hang)
        assert killed == [["docker", "kill", container_name(SCAN_ID)]]

    def test_missing_licence_file_fails_before_docker(self, tmp_path):
        settings = _settings(tmp_path, fs_license_path=str(tmp_path / "no-such-licence.txt"))
        with pytest.raises(FastSurferError, match="FS_LICENSE_PATH"):
            run_fastsurfer(_nifti(tmp_path), SCAN_ID, settings, run=_fake_docker())

    def test_missing_docker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vp.shutil, "which", lambda name: None)
        with pytest.raises(FastSurferError, match="Docker is not installed"):
            run_fastsurfer(_nifti(tmp_path), SCAN_ID, _settings(tmp_path), run=_fake_docker())

    def test_missing_scan_file(self, tmp_path):
        with pytest.raises(FastSurferError, match="stored scan file is missing"):
            run_fastsurfer(
                tmp_path / "gone.nii.gz", SCAN_ID, _settings(tmp_path), run=_fake_docker()
            )


class TestLogTail:
    def test_last_non_empty_line(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_bytes(b"one\ntwo\n\n   \n")
        assert read_log_tail(log) == "two"

    def test_invalid_bytes_are_replaced(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_bytes(b"\xff\xfe broken \x80\n")
        assert "broken" in read_log_tail(log)

    def test_long_line_is_truncated(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_bytes(b"e" * 5000)
        assert len(read_log_tail(log)) == 200

    def test_missing_and_empty_logs(self, tmp_path):
        assert read_log_tail(tmp_path / "none.log") == ""
        (tmp_path / "empty.log").write_bytes(b"")
        assert read_log_tail(tmp_path / "empty.log") == ""


async def _segmenting_scan(app_db, tmp_path, *, source=DataSource.CLINIC) -> uuid.UUID:
    async with app_db() as session:
        patient = await patient_repo.create(session, age=70, sex=Sex.M, source=source)
        scan = await patient_repo.create_scan(
            session,
            patient_id=patient.id,
            scan_date=datetime.date.today(),
            file_path=str(_nifti(tmp_path)),
            external_subject_id="CLINIC-0042",
        )
        scan.status = ScanStatus.SEGMENTING
        scan.extraction_method = ExtractionMethod.FASTSURFER
        await session.commit()
        return scan.id


async def _load(app_db, scan_id):
    async with app_db() as session:
        return await patient_repo.get_scan(session, scan_id)


class TestFastSurferJob:
    async def test_job_moves_segmenting_to_completed(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)
        received = []

        def runner(nifti_path, job_scan_id, settings):
            received.append((nifti_path.name, job_scan_id))
            return FASTSURFER_STATS

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)

        scan = await _load(app_db, scan_id)
        assert received == [("scan.nii.gz", scan_id)]
        assert scan.status == ScanStatus.COMPLETED
        assert scan.failure_reason is None
        assert scan.extraction_method == ExtractionMethod.FASTSURFER
        assert scan.external_subject_id == "CLINIC-0042"
        assert len(scan.biomarkers) == 3
        assert scan.raw_volumes["icv_mm3"] == 1600000.0
        assert scan.fis_result is not None
        # Real data, phantom calibration: scored but not staged.
        assert scan.fis_result.determinate is False

    async def test_status_passes_through_running_inference(self, app_db, tmp_path, monkeypatch):
        scan_id = await _segmenting_scan(app_db, tmp_path)
        seen = []
        original = vp.scan_service.process_derived_biomarkers

        async def spy(db, scan, *args, **kwargs):
            seen.append((await _load(app_db, scan.id)).status)
            return await original(db, scan, *args, **kwargs)

        monkeypatch.setattr(vp.scan_service, "process_derived_biomarkers", spy)
        await run_fastsurfer_job(
            scan_id, session_factory=app_db, runner=lambda *a: FASTSURFER_STATS
        )
        # Committed, so a client polling the scan can see it.
        assert seen == [ScanStatus.RUNNING_INFERENCE]

    async def test_fastsurfer_failure_is_stored_as_the_failure_reason(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)

        def runner(*args):
            raise FastSurferError("FastSurfer exited with status 1. Last log line: out of memory")

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)

        scan = await _load(app_db, scan_id)
        assert scan.status == ScanStatus.FAILED
        assert scan.failure_reason == (
            "FastSurfer exited with status 1. Last log line: out of memory"
        )
        assert scan.fis_result is None
        assert scan.biomarkers == []

    async def test_unparseable_stats_fail_the_scan_with_the_parser_message(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)
        empty = tmp_path / "stats"
        empty.mkdir()

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=lambda *a: empty)

        scan = await _load(app_db, scan_id)
        assert scan.status == ScanStatus.FAILED
        assert scan.failure_reason.startswith("No aseg stats file was found")

    async def test_missing_icv_fails_with_a_specific_reason(self, app_db, tmp_path):
        """What a FastSurfer run without Talairach registration looks like."""
        scan_id = await _segmenting_scan(app_db, tmp_path)
        stats = tmp_path / "stats"
        stats.mkdir()
        text = (FASTSURFER_STATS / "aseg+DKT.VINN.stats").read_text(encoding="utf-8")
        (stats / "aseg+DKT.VINN.stats").write_text(
            "\n".join(line for line in text.splitlines() if "eTIV" not in line), encoding="utf-8"
        )

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=lambda *a: stats)

        scan = await _load(app_db, scan_id)
        assert scan.status == ScanStatus.FAILED
        assert "has no intracranial volume" in scan.failure_reason

    async def test_unexpected_crash_fails_the_scan_with_a_generic_reason(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)

        def runner(*args):
            raise RuntimeError("internal detail that must not be shown: /srv/secret/path")

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)

        scan = await _load(app_db, scan_id)
        assert scan.status == ScanStatus.FAILED
        assert scan.failure_reason == "Segmentation failed with an unexpected server error."

    async def test_long_failure_reason_is_truncated_to_fit(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)

        def runner(*args):
            raise FastSurferError("x" * 2000)

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)
        assert len((await _load(app_db, scan_id)).failure_reason) == 500

    async def test_job_is_idempotent(self, app_db, tmp_path):
        scan_id = await _segmenting_scan(app_db, tmp_path)
        calls = []

        def runner(*args):
            calls.append(1)
            return FASTSURFER_STATS

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)
        first = await _load(app_db, scan_id)
        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=runner)
        second = await _load(app_db, scan_id)

        assert calls == [1]  # the second call found nothing to do
        assert second.status == ScanStatus.COMPLETED
        assert second.fis_result.id == first.fis_result.id
        assert len(second.biomarkers) == 3

    @pytest.mark.parametrize(
        "status",
        [
            ScanStatus.COMPLETED,
            ScanStatus.FAILED,
            ScanStatus.AWAITING_VOLUMETRY,
            ScanStatus.PENDING,
        ],
    )
    async def test_job_ignores_a_scan_that_is_not_segmenting(self, app_db, tmp_path, status):
        scan_id = await _segmenting_scan(app_db, tmp_path)
        async with app_db() as session:
            scan = await patient_repo.get_scan(session, scan_id)
            scan.status = status
            await session.commit()

        def must_not_run(*args):
            raise AssertionError("segmentation started for a scan that is not segmenting")

        await run_fastsurfer_job(scan_id, session_factory=app_db, runner=must_not_run)
        assert (await _load(app_db, scan_id)).status == status

    async def test_job_for_an_unknown_scan_is_a_no_op(self, app_db):
        await run_fastsurfer_job(uuid.uuid4(), session_factory=app_db, runner=lambda *a: None)


class TestLaunch:
    async def test_launching_twice_returns_the_running_task(self, monkeypatch):
        started = []
        release = asyncio.Event()

        async def slow_job(scan_id):
            started.append(scan_id)
            await release.wait()

        monkeypatch.setattr(vp, "run_fastsurfer_job", slow_job)

        first = launch_fastsurfer_job(SCAN_ID)
        second = launch_fastsurfer_job(SCAN_ID)
        await asyncio.sleep(0)

        assert first is second
        assert started == [SCAN_ID]

        release.set()
        await first
        await asyncio.sleep(0)
        assert SCAN_ID not in vp._jobs

    async def test_launch_does_not_block(self, monkeypatch):
        release = asyncio.Event()

        async def slow_job(scan_id):
            await release.wait()

        monkeypatch.setattr(vp, "run_fastsurfer_job", slow_job)
        task = launch_fastsurfer_job(uuid.uuid4())
        assert not task.done()  # returned while the job is still running
        release.set()
        await task

    async def test_a_finished_job_can_be_launched_again(self, monkeypatch):
        runs = []

        async def quick_job(scan_id):
            runs.append(scan_id)

        monkeypatch.setattr(vp, "run_fastsurfer_job", quick_job)
        await launch_fastsurfer_job(SCAN_ID)
        await asyncio.sleep(0)
        await launch_fastsurfer_job(SCAN_ID)
        assert runs == [SCAN_ID, SCAN_ID]


class TestInterruptedJobs:
    async def test_stranded_segmenting_scans_are_failed_at_startup(self, app_db, tmp_path):
        stranded = await _segmenting_scan(app_db, tmp_path)
        async with app_db() as session:
            patient = await patient_repo.create(
                session, age=60, sex=Sex.F, source=DataSource.CLINIC
            )
            waiting = await patient_repo.create_scan(
                session, patient_id=patient.id, scan_date=datetime.date.today(), file_path=None
            )
            waiting.status = ScanStatus.AWAITING_VOLUMETRY
            await session.commit()
            waiting_id = waiting.id

        async with app_db() as session:
            assert await fail_interrupted_jobs(session) == 1

        scan = await _load(app_db, stranded)
        assert scan.status == ScanStatus.FAILED
        assert "interrupted by a server restart" in scan.failure_reason
        assert (await _load(app_db, waiting_id)).status == ScanStatus.AWAITING_VOLUMETRY

    async def test_nothing_stranded(self, app_db):
        async with app_db() as session:
            assert await fail_interrupted_jobs(session) == 0


class TestSubmitStoredScan:
    async def _scan(self, db_session, source):
        patient = await patient_repo.create(db_session, age=70, sex=Sex.F, source=source)
        scan = await patient_repo.create_scan(
            db_session, patient_id=patient.id, scan_date=datetime.date.today(), file_path="x.nii.gz"
        )
        return scan

    async def test_external_leaves_the_scan_awaiting_volumetry(self, db_session):
        scan = await self._scan(db_session, DataSource.CLINIC)
        result = await submit_stored_scan(
            db_session, scan, Path("x.nii.gz"), VolumetryProvider.EXTERNAL
        )
        assert result.status == ScanStatus.AWAITING_VOLUMETRY
        assert result.extraction_method is None
        assert result.fis_result is None

    async def test_fastsurfer_marks_segmenting_and_launches_once(self, db_session):
        scan = await self._scan(db_session, DataSource.CLINIC)
        launched = []
        result = await submit_stored_scan(
            db_session,
            scan,
            Path("x.nii.gz"),
            VolumetryProvider.FASTSURFER_DOCKER,
            launch=launched.append,
        )
        assert result.status == ScanStatus.SEGMENTING
        assert result.extraction_method == ExtractionMethod.FASTSURFER
        assert launched == [scan.id]
