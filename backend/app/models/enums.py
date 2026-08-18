from enum import StrEnum


class UserRole(StrEnum):
    CLINICIAN = "clinician"
    ADMIN = "admin"


class Sex(StrEnum):
    F = "F"
    M = "M"


class DataSource(StrEnum):
    OASIS = "OASIS"
    ADNI = "ADNI"
    CLINIC = "Clinic"


class ScanStatus(StrEnum):
    PENDING = "pending"
    PREPROCESSING = "preprocessing"
    EXTRACTING_BIOMARKERS = "extracting_biomarkers"
    RUNNING_INFERENCE = "running_inference"
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
