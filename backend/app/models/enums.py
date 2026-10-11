from enum import StrEnum


class UserRole(StrEnum):
    CLINICIAN = "clinician"
    ADMIN = "admin"
    # May read the research export (reference labels, ratings). Does not
    # rate cases: a researcher has seen the answers.
    RESEARCHER = "researcher"


class Sex(StrEnum):
    F = "F"
    M = "M"


class DataSource(StrEnum):
    # C1 (closed): synthetic phantom cases carry their own provenance value
    # and are never recorded as OASIS/ADNI/Clinic. A patient is PHANTOM if,
    # and only if, every scan on it came from the synthetic phantom; see
    # app/services/scan_service.py for where that invariant is enforced.
    OASIS = "OASIS"
    ADNI = "ADNI"
    CLINIC = "Clinic"
    PHANTOM = "PHANTOM"


class ExtractionMethod(StrEnum):
    """How a scan's biomarkers were measured. Recorded on every scan so a
    result can always be traced back to the instrument that produced it.
    """

    INHOUSE_PHANTOM = "inhouse_phantom"  # app/services/volumetry.py, phantom-tuned
    FREESURFER_STATS = "freesurfer_stats"  # uploaded aseg/aparc stats files
    FASTSURFER = "fastsurfer"  # FastSurfer run by the volumetry provider
    MANUAL_BIOMARKERS = "manual_biomarkers"  # values posted as JSON / CSV import


class ScanStatus(StrEnum):
    PENDING = "pending"
    PREPROCESSING = "preprocessing"
    EXTRACTING_BIOMARKERS = "extracting_biomarkers"
    RUNNING_INFERENCE = "running_inference"
    # The scan file is stored and waiting for FreeSurfer stats produced
    # outside this service (VOLUMETRY_PROVIDER=external).
    AWAITING_VOLUMETRY = "awaiting_volumetry"
    # FastSurfer is running as a background job (VOLUMETRY_PROVIDER=fastsurfer_docker).
    SEGMENTING = "segmenting"
    COMPLETED = "completed"
    FAILED = "failed"


class Stage(StrEnum):
    CN = "CN"
    MCI = "MCI"
    AD = "AD"


class BiomarkerKey(StrEnum):
    HIPPOCAMPAL_VOLUME = "hippocampal_volume"
    VENTRICLE_BRAIN_RATIO = "ventricle_brain_ratio"
    CORTICAL_THICKNESS = "cortical_thickness"
