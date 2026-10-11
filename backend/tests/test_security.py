"""Phase 6: authentication mode, roles, rate limits and error hygiene."""

import os
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from structlog.testing import capture_logs

from app.cli import create_user as user_cli
from app.core.config import DEFAULT_SECRET_KEY, Settings, get_settings
from app.core.startup_checks import (
    DEMO_MODE_WARNING,
    UnsafeConfigurationError,
    ensure_upload_dir_not_served,
    log_security_posture,
)
from app.main import app as fastapi_app
from app.models.audit import AuditLog
from app.models.enums import UserRole
from app.models.user import User
from tests.conftest import login_as, register_and_login

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.fixture
def auth_required(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_mode", "required")
    monkeypatch.setattr(get_settings(), "allow_self_registration", False)


class TestAuthModeSetting:
    def test_development_defaults_to_demo(self):
        settings = _settings(environment="development")
        assert settings.auth_mode == "demo"
        assert settings.allow_self_registration is True

    def test_test_environment_defaults_to_demo(self):
        assert _settings(environment="test").auth_mode == "demo"

    def test_production_defaults_to_required(self):
        settings = _settings(environment="production", secret_key="a-real-secret-value")
        assert settings.auth_mode == "required"
        assert settings.allow_self_registration is False

    def test_required_can_be_chosen_outside_production(self):
        settings = _settings(environment="development", auth_mode="required")
        assert settings.auth_mode == "required"
        assert settings.allow_self_registration is False

    def test_demo_in_production_refuses_to_load(self):
        with pytest.raises(ValidationError, match="AUTH_MODE=demo is not allowed"):
            _settings(environment="production", auth_mode="demo", secret_key="a-real-secret-value")

    def test_placeholder_secret_in_production_refuses_to_load(self):
        with pytest.raises(
            ValidationError, match="SECRET_KEY is still the development placeholder"
        ):
            _settings(environment="production", secret_key=DEFAULT_SECRET_KEY)

    def test_a_settings_error_does_not_print_the_secret(self):
        with pytest.raises(ValidationError) as excinfo:
            _settings(environment="production", auth_mode="demo", secret_key="s3cr3t-signing-key")
        assert "s3cr3t-signing-key" not in str(excinfo.value)
        assert "s3cr3t-signing-key" not in repr(excinfo.value)

    def test_placeholder_secret_is_fine_in_development(self):
        assert _settings(environment="development").secret_key == DEFAULT_SECRET_KEY

    def test_blank_value_means_unset(self, monkeypatch):
        """`AUTH_MODE=` in a compose or env file must not be a parse error."""
        monkeypatch.setenv("AUTH_MODE", "")
        monkeypatch.setenv("FS_LICENSE_PATH", "  ")
        settings = _settings(environment="development")
        assert settings.auth_mode == "demo"
        assert settings.fs_license_path is None

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValidationError):
            _settings(auth_mode="off")

    def test_self_registration_can_be_set_explicitly(self):
        settings = _settings(auth_mode="required", allow_self_registration=True)
        assert settings.allow_self_registration is True


class TestRefusesToStart:
    def _import_app(self, **env) -> subprocess.CompletedProcess:
        full_env = {**os.environ, **env}
        return subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_DIR,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_demo_mode_in_production_stops_the_process(self):
        result = self._import_app(
            ENVIRONMENT="production", AUTH_MODE="demo", SECRET_KEY="a-real-secret-value"
        )
        assert result.returncode != 0
        assert "AUTH_MODE=demo is not allowed with ENVIRONMENT=production" in result.stderr

    def test_production_with_required_auth_starts(self):
        result = self._import_app(
            ENVIRONMENT="production", AUTH_MODE="required", SECRET_KEY="a-real-secret-value"
        )
        assert result.returncode == 0, result.stderr


class TestStartupLogging:
    def test_demo_mode_logs_one_clear_warning(self):
        with capture_logs() as logs:
            log_security_posture(_settings(environment="development"))
        assert len(logs) == 1
        assert logs[0]["log_level"] == "warning"
        assert logs[0]["event"] == "auth_demo_mode"
        assert logs[0]["detail"] == DEMO_MODE_WARNING
        assert "Do not load real patient data" in DEMO_MODE_WARNING

    def test_required_mode_does_not_warn(self):
        with capture_logs() as logs:
            log_security_posture(_settings(auth_mode="required"))
        assert [entry["log_level"] for entry in logs] == ["info"]


class TestDemoMode:
    async def test_no_token_acts_as_the_shared_demo_user(self, client: AsyncClient):
        assert get_settings().auth_mode == "demo"
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == "demo@fade.local"
        assert resp.json()["role"] == "clinician"

    async def test_a_token_is_still_honoured(self, client: AsyncClient):
        headers = await register_and_login(client, "real@test.dev")
        assert (await client.get("/api/v1/auth/me", headers=headers)).json()["email"] == (
            "real@test.dev"
        )

    async def test_role_checks_still_apply(self, client: AsyncClient):
        """Demo mode supplies an identity; it does not grant roles."""
        assert (await client.get("/api/v1/export/results.csv")).status_code == 403


PROTECTED = [
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/patients"),
    ("POST", "/api/v1/patients"),
    ("GET", "/api/v1/patients/00000000-0000-0000-0000-000000000000"),
    ("GET", "/api/v1/cohort/stats"),
    ("GET", "/api/v1/fis/rules"),
    ("GET", "/api/v1/fis/biomarkers"),
    ("POST", "/api/v1/fis/simulate"),
    ("GET", "/api/v1/study/cases"),
    ("POST", "/api/v1/study/cases/00000000-0000-0000-0000-000000000000/rating"),
    ("GET", "/api/v1/export/results.csv"),
    ("POST", "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/synthetic"),
    ("POST", "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/biomarkers"),
    (
        "GET",
        "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/"
        "00000000-0000-0000-0000-000000000001",
    ),
]
PROTECTED_UPLOADS = [
    "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/upload",
    "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/upload-dicom",
    "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/freesurfer",
    (
        "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/"
        "00000000-0000-0000-0000-000000000001/stats"
    ),
]


@pytest.mark.usefixtures("auth_required")
class TestRequiredMode:
    @pytest.mark.parametrize(("method", "path"), PROTECTED)
    async def test_no_token_is_401(self, client: AsyncClient, method, path):
        resp = await client.request(method, path, json={})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    @pytest.mark.parametrize("path", PROTECTED_UPLOADS)
    async def test_uploads_need_a_token_too(self, client: AsyncClient, path):
        resp = await client.post(path, files={"file": ("x", b"x"), "aseg": ("aseg.stats", b"x")})
        assert resp.status_code == 401

    async def test_no_default_user_is_created(self, client: AsyncClient, app_db):
        await client.get("/api/v1/patients")
        async with app_db() as session:
            assert (await session.execute(select(User))).scalars().all() == []

    async def test_health_stays_open(self, client: AsyncClient):
        assert (await client.get("/api/v1/health")).status_code == 200

    async def test_a_valid_token_is_accepted(self, client: AsyncClient, app_db):
        headers = await login_as(client, app_db, UserRole.CLINICIAN)
        resp = await client.get("/api/v1/patients", headers=headers)
        assert resp.status_code == 200
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["email"] == "clinician@test.dev"

    async def test_a_bad_token_is_401(self, client: AsyncClient):
        resp = await client.get(
            "/api/v1/patients", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 401

    async def test_a_deactivated_account_is_401(self, client: AsyncClient, app_db):
        headers = await login_as(client, app_db, UserRole.CLINICIAN)
        async with app_db() as session:
            user = (await session.execute(select(User))).scalar_one()
            user.is_active = False
            await session.commit()
        assert (await client.get("/api/v1/patients", headers=headers)).status_code == 401

    async def test_self_registration_is_closed(self, client: AsyncClient, app_db):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "walkin@test.dev", "password": "supersecret1", "full_name": "Walk In"},
        )
        assert resp.status_code == 403
        assert "Self-registration is disabled" in resp.json()["error"]["message"]
        async with app_db() as session:
            assert (await session.execute(select(User))).scalars().all() == []

    async def test_roles_are_enforced_with_tokens(self, client: AsyncClient, app_db):
        clinician = await login_as(client, app_db, UserRole.CLINICIAN)
        researcher = await login_as(client, app_db, UserRole.RESEARCHER)

        assert (
            await client.get("/api/v1/export/results.csv", headers=clinician)
        ).status_code == 403
        assert (
            await client.get("/api/v1/export/results.csv", headers=researcher)
        ).status_code == 200
        assert (await client.get("/api/v1/study/cases", headers=researcher)).status_code == 403
        assert (await client.get("/api/v1/study/cases", headers=clinician)).status_code == 200


class TestUploadDirectoryIsNotServed:
    def test_the_real_app_serves_no_static_files_over_uploads(self):
        ensure_upload_dir_not_served(fastapi_app, get_settings())

    def test_upload_dir_inside_a_static_mount_is_refused(self, tmp_path):
        static = tmp_path / "static"
        (static / "uploads").mkdir(parents=True)
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=static), name="static")

        with pytest.raises(UnsafeConfigurationError, match="must not be served as static files"):
            ensure_upload_dir_not_served(app, _settings(upload_dir=str(static / "uploads")))

    def test_upload_dir_that_is_the_static_mount_is_refused(self, tmp_path):
        app = FastAPI()
        app.mount("/files", StaticFiles(directory=tmp_path), name="files")
        with pytest.raises(UnsafeConfigurationError):
            ensure_upload_dir_not_served(app, _settings(upload_dir=str(tmp_path)))

    def test_static_mount_inside_the_upload_dir_is_refused(self, tmp_path):
        (tmp_path / "uploads" / "public").mkdir(parents=True)
        app = FastAPI()
        app.mount("/p", StaticFiles(directory=tmp_path / "uploads" / "public"), name="p")
        with pytest.raises(UnsafeConfigurationError):
            ensure_upload_dir_not_served(app, _settings(upload_dir=str(tmp_path / "uploads")))

    def test_unrelated_static_mount_is_fine(self, tmp_path):
        (tmp_path / "static").mkdir()
        (tmp_path / "uploads").mkdir()
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=tmp_path / "static"), name="static")
        ensure_upload_dir_not_served(app, _settings(upload_dir=str(tmp_path / "uploads")))


def _nifti_upload(tmp_path: Path) -> dict:
    path = tmp_path / "s.nii.gz"
    nib.save(nib.Nifti1Image(np.ones((8, 8, 8), dtype=np.int16), np.eye(4)), str(path))
    return {"file": ("s.nii.gz", path.read_bytes(), "application/octet-stream")}


@pytest.mark.usefixtures("rate_limits")
class TestRateLimits:
    async def test_default_limit_applies_to_ordinary_routes(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "3/minute")

        statuses = [(await client.get("/api/v1/patients")).status_code for _ in range(5)]
        assert statuses == [200, 200, 200, 429, 429]

    async def test_429_uses_the_api_error_shape(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1/minute")
        await client.get("/api/v1/patients")
        resp = await client.get("/api/v1/patients")

        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limited"
        assert "Too many requests" in resp.json()["error"]["message"]

    async def test_a_429_still_carries_cors_headers(self, client: AsyncClient, monkeypatch):
        """Without them a browser reports a network error, not "slow down"."""
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1/minute")
        origin = get_settings().cors_allow_origins[0]
        await client.get("/api/v1/patients", headers={"Origin": origin})
        resp = await client.get("/api/v1/patients", headers={"Origin": origin})

        assert resp.status_code == 429
        assert resp.headers["access-control-allow-origin"] == origin

    async def test_limit_is_counted_per_route(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1/minute")
        assert (await client.get("/api/v1/patients")).status_code == 200
        assert (await client.get("/api/v1/patients")).status_code == 429
        # A different route has its own count.
        assert (await client.get("/api/v1/cohort/stats")).status_code == 200

    async def test_upload_endpoints_have_their_own_tighter_limit(
        self, client: AsyncClient, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1000/minute")
        monkeypatch.setattr(get_settings(), "upload_rate_limit", "2/minute")
        monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
        patient = (
            await client.post("/api/v1/patients", json={"age": 70, "sex": "F", "source": "Clinic"})
        ).json()
        url = f"/api/v1/patients/{patient['id']}/scans/upload"

        statuses = [
            (await client.post(url, files=_nifti_upload(tmp_path))).status_code for _ in range(4)
        ]
        assert statuses == [201, 201, 429, 429]
        # Other routes are not held to the upload limit.
        assert (await client.get("/api/v1/patients")).status_code == 200

    @pytest.mark.parametrize("endpoint", ["upload-dicom", "freesurfer"])
    async def test_every_file_endpoint_is_limited(self, client: AsyncClient, monkeypatch, endpoint):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1000/minute")
        monkeypatch.setattr(get_settings(), "upload_rate_limit", "1/minute")
        patient = (
            await client.post("/api/v1/patients", json={"age": 70, "sex": "F", "source": "Clinic"})
        ).json()
        url = f"/api/v1/patients/{patient['id']}/scans/{endpoint}"
        files = {"file": ("x.zip", b"junk"), "aseg": ("aseg.stats", b"junk")}

        first = await client.post(url, files=files)
        second = await client.post(url, files=files)
        assert first.status_code == 422  # reached the endpoint, bad file
        assert second.status_code == 429  # refused before the file was read

    async def test_stats_attachment_is_limited(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1000/minute")
        monkeypatch.setattr(get_settings(), "upload_rate_limit", "1/minute")
        url = (
            "/api/v1/patients/00000000-0000-0000-0000-000000000000/scans/"
            "00000000-0000-0000-0000-000000000001/stats"
        )
        files = {"aseg": ("aseg.stats", b"junk")}
        assert (await client.post(url, files=files)).status_code == 404
        assert (await client.post(url, files=files)).status_code == 429


class TestRateLimitsAreOffByDefaultInTests:
    async def test_many_requests_pass(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(get_settings(), "rate_limit_default", "1/minute")
        statuses = {(await client.get("/api/v1/patients")).status_code for _ in range(5)}
        assert statuses == {200}


class TestValidationErrorsDoNotEchoInput:
    async def test_rejected_json_body_is_not_repeated_back(self, client: AsyncClient):
        patient = (
            await client.post("/api/v1/patients", json={"age": 70, "sex": "F", "source": "Clinic"})
        ).json()
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/biomarkers",
            json={
                "derived": {"corticalThickness": {"value": 2.8, "unit": "mm"}},
                "externalSubjectId": "Jane Q Public 1940-01-01",
            },
        )
        assert resp.status_code == 422
        assert "Jane" not in resp.text
        assert "1940" not in resp.text

    async def test_rejected_rating_note_is_not_repeated_back(self, client: AsyncClient):
        note = "Mrs Public's daughter reports " + "x" * 2000
        resp = await client.post(
            "/api/v1/study/cases/00000000-0000-0000-0000-000000000000/rating",
            json={"stageCalled": "MCI", "borderline": False, "confidence": 3, "notes": note},
        )
        assert resp.status_code == 422
        assert "Mrs Public" not in resp.text

    async def test_error_detail_keeps_its_list_shape(self, client: AsyncClient):
        resp = await client.post("/api/v1/patients", json={"age": 999, "sex": "F"})
        assert resp.status_code == 422
        (error,) = resp.json()["detail"]
        assert set(error) == {"type", "loc", "msg"}
        assert error["loc"] == ["body", "age"]
        assert "less than or equal to 120" in error["msg"]


class TestCreateUserCommand:
    async def test_creates_an_account_with_the_requested_role(self, db_session):
        user = await user_cli.create_user(
            db_session,
            email="ml@test.dev",
            full_name="ML Teammate",
            role=UserRole.RESEARCHER,
            password="a-long-password",
        )
        await db_session.commit()

        assert user.role == UserRole.RESEARCHER
        assert user.is_active is True
        assert user.hashed_password != "a-long-password"
        (entry,) = (await db_session.execute(select(AuditLog))).scalars().all()
        assert entry.action == "user.create"
        assert entry.detail == {"role": "researcher"}
        assert entry.entity_id == str(user.id)

    async def test_created_account_can_log_in_and_export(self, client: AsyncClient, app_db):
        async with app_db() as session:
            await user_cli.create_user(
                session,
                email="ml@test.dev",
                full_name="ML Teammate",
                role=UserRole.RESEARCHER,
                password="a-long-password",
            )
            await session.commit()

        login = await client.post(
            "/api/v1/auth/login", json={"email": "ml@test.dev", "password": "a-long-password"}
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        assert (await client.get("/api/v1/export/results.csv", headers=headers)).status_code == 200

    async def test_short_password_and_duplicate_email_are_refused(self, db_session):
        with pytest.raises(user_cli.UserCommandError, match="at least 8 characters"):
            await user_cli.create_user(
                db_session, email="a@test.dev", full_name="A", role=UserRole.ADMIN, password="short"
            )
        await user_cli.create_user(
            db_session,
            email="a@test.dev",
            full_name="A",
            role=UserRole.ADMIN,
            password="long-enough-1",
        )
        with pytest.raises(user_cli.UserCommandError, match="already exists"):
            await user_cli.create_user(
                db_session,
                email="a@test.dev",
                full_name="A",
                role=UserRole.ADMIN,
                password="long-enough-1",
            )

    async def test_set_role_changes_and_audits(self, db_session):
        await user_cli.create_user(
            db_session,
            email="a@test.dev",
            full_name="A",
            role=UserRole.CLINICIAN,
            password="long-enough-1",
        )
        user = await user_cli.set_role(db_session, email="a@test.dev", role=UserRole.ADMIN)
        await db_session.commit()

        assert user.role == UserRole.ADMIN
        entries = (
            (await db_session.execute(select(AuditLog).order_by(AuditLog.created_at)))
            .scalars()
            .all()
        )
        assert entries[-1].action == "user.role_change"
        assert entries[-1].detail == {"role": "admin", "previous_role": "clinician"}

    async def test_set_role_on_an_unknown_account(self, db_session):
        with pytest.raises(user_cli.UserCommandError, match="No account"):
            await user_cli.set_role(db_session, email="nobody@test.dev", role=UserRole.ADMIN)

    def test_password_comes_from_the_environment_not_the_command_line(self, monkeypatch):
        monkeypatch.setenv("NEW_USER_PW", "from-the-environment")
        assert user_cli.read_password("NEW_USER_PW") == "from-the-environment"
        monkeypatch.delenv("NEW_USER_PW")
        with pytest.raises(user_cli.UserCommandError, match="is not set"):
            user_cli.read_password("NEW_USER_PW")

        options = {a.dest for a in user_cli.build_parser()._actions}
        assert "password" not in options

    def test_prompted_passwords_must_match(self, monkeypatch):
        answers = iter(["one-password", "another-password"])
        monkeypatch.setattr(user_cli.getpass, "getpass", lambda prompt: next(answers))
        with pytest.raises(user_cli.UserCommandError, match="do not match"):
            user_cli.read_password(None)

    def test_name_is_required_to_create(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            user_cli.main(["--email", "a@test.dev", "--role", "admin"])
        assert excinfo.value.code == 2
        assert "--name is required" in capsys.readouterr().err

    def test_main_reports_failure_with_a_non_zero_exit(self, monkeypatch, capsys):
        async def _failing(args):
            raise user_cli.UserCommandError("An account with this email already exists.")

        monkeypatch.setattr(user_cli, "_run", _failing)
        monkeypatch.setattr(user_cli, "configure_logging", lambda: None)
        levels = [
            user_cli.logging.getLogger().level,
            user_cli.logging.getLogger("sqlalchemy.engine").level,
        ]
        try:
            code = user_cli.main(["--email", "a@test.dev", "--name", "A", "--role", "admin"])
        finally:
            user_cli.logging.getLogger().setLevel(levels[0])
            user_cli.logging.getLogger("sqlalchemy.engine").setLevel(levels[1])
        assert code == user_cli.EXIT_ERROR
        assert "already exists" in capsys.readouterr().err
