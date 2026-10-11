from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration, sourced from environment / .env.

    Nothing here is hardcoded elsewhere — services and routers pull settings
    through `get_settings()` so the whole app is configurable per-environment
    without code changes.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App metadata ---
    app_name: str = "FADE API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    # Defaults to a local SQLite file so the API runs with zero external
    # dependencies; point DATABASE_URL at Postgres (asyncpg) in production.
    database_url: str = "sqlite+aiosqlite:///./fade.db"
    database_echo: bool = False

    # --- Security / auth ---
    secret_key: str = Field(
        default="dev-only-insecure-secret-change-me-via-SECRET_KEY-env-var",
        description="HMAC signing key for JWTs. MUST be overridden in production.",
    )
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12
    refresh_token_expire_minutes: int = 60 * 24 * 7

    # --- CORS ---
    cors_allow_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- Rate limiting ---
    rate_limit_default: str = "120/minute"

    # --- Clinical thresholds (kept configurable, not magic numbers buried in services) ---
    # TODO(clinical: C6) -- scale and polarity differ from the specified
    # "review when confidence < 0.65". This is an UNCERTAINTY threshold on
    # a 0-100 scale, compared with >=. Provisional pending calibration.
    uncertainty_review_threshold: float = 45.0

    # --- Calibration ---
    # The file holding every fuzzy-set breakpoint, unit and output set (see
    # app/services/calibration.py). A bare name is looked up in
    # app/calibration/; a path is used as given. The default is fitted to the
    # synthetic phantom, so real data stays indeterminate until a calibration
    # with "domain": "real" is supplied here.
    calibration_file: str = "phantom.v0.json"

    # --- Biomarker derivation from FreeSurfer / FastSurfer stats ---
    # Regions whose ThickAvg is averaged, over both hemispheres, into the
    # cortical_thickness input. Names as they appear in aparc.stats.
    cortical_thickness_regions: list[str] = [
        "entorhinal",
        "parahippocampal",
        "superiortemporal",
        "middletemporal",
        "inferiortemporal",
    ]
    # TODO(confirm): "unweighted" (plain mean of ThickAvg) was specified;
    # "surface_area" weights each region by SurfArea. ML team's call.
    cortical_thickness_weighting: Literal["unweighted", "surface_area"] = "unweighted"

    # Physiological plausibility bounds, as (min, max). A value outside its
    # bounds does not fail the scan: it raises a QC flag and the engine
    # returns an indeterminate result. Initial bounds supplied by the team.
    plausibility_icv_mm3: tuple[float, float] = (1.0e6, 2.2e6)
    plausibility_hippocampus_total_mm3: tuple[float, float] = (2000.0, 12000.0)
    plausibility_thickness_mm: tuple[float, float] = (1.0, 5.0)

    # Upper size for one uploaded stats file. Real ones are 5-30 KB.
    max_stats_upload_kb: int = 1024

    # --- File uploads ---
    max_upload_mb: int = 64
    upload_dir: str = "./data/uploads"

    # Gross sanity limits on an uploaded NIfTI volume. Technical, not
    # clinical: they catch a header in the wrong units or a decompression
    # bomb, not a scan that is merely low resolution.
    nifti_voxel_mm_range: tuple[float, float] = (0.1, 10.0)
    nifti_max_voxels: int = 512 * 512 * 512

    # --- DICOM uploads ---
    # With this on (the default), a DICOM whose identifying tags are not
    # blank is rejected before conversion. Turn it off only for data that
    # is already consented for identified use; values are never logged.
    require_anonymised_dicom: bool = True
    dicom_identifying_tags: list[str] = [
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "OtherPatientIDs",
        "InstitutionName",
        "ReferringPhysicianName",
        "AccessionNumber",
        "StudyDate",
    ]
    dicom_zip_max_files: int = 5000
    dicom_zip_max_unpacked_mb: int = 2048
    dicom_conversion_timeout_seconds: int = 600

    # --- Volumetry provider: what happens to an uploaded scan file ---
    # external          store it and wait for FreeSurfer stats to be attached
    # fastsurfer_docker run FastSurfer in Docker as a background job
    # inhouse           the phantom-tuned volumetry; PHANTOM patients only
    volumetry_provider: Literal["external", "fastsurfer_docker", "inhouse"] = "external"

    fastsurfer_image: str = "deepmi/fastsurfer:latest"
    fastsurfer_use_gpu: bool = False
    fastsurfer_timeout_seconds: int = 3 * 60 * 60
    # FreeSurfer licence file on the host, mounted read-only into the container.
    fs_license_path: str | None = None
    # "uid:gid" the container runs as. Default: this process's own ids on
    # POSIX, 1000:1000 on Windows (where Docker Desktop ignores ownership).
    fastsurfer_docker_user: str | None = None
    # Appended to the FastSurfer command line, e.g. ["--threads", "4"].
    fastsurfer_extra_args: list[str] = []
    # Where FastSurfer writes. Default: <upload_dir>/fastsurfer.
    fastsurfer_output_dir: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
