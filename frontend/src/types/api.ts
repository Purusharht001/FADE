/**
 * Hand-written mirror of the backend's Pydantic response/request schemas
 * (backend/app/schemas/*.py). The backend is the only place that computes
 * anything clinical — these types describe the wire contract, nothing more.
 * Field names are camelCase because every backend schema inherits from
 * `CamelModel` (backend/app/schemas/base.py), which serializes with a
 * camelCase alias generator specifically so this file doesn't have to
 * transform anything.
 */

export type Stage = "CN" | "MCI" | "AD";

export type BiomarkerKey = "hippocampal_volume" | "ventricle_brain_ratio" | "cortical_thickness";

export type FuzzyLabel = "low" | "borderline" | "normal" | "high";

export type Sex = "F" | "M";

export type DataSource = "OASIS" | "ADNI" | "Clinic";

export type UserRole = "clinician" | "admin";

export type ScanStatus =
  | "pending"
  | "preprocessing"
  | "extracting_biomarkers"
  | "running_inference"
  | "completed"
  | "failed";

// --- Auth ---

export interface UserRead {
  id: string;
  email: string;
  fullName: string;
  role: UserRole;
  isActive: boolean;
}

export interface TokenPair {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
}

export interface UserRegisterRequest {
  email: string;
  password: string;
  fullName: string;
}

export interface UserLoginRequest {
  email: string;
  password: string;
}

// --- FIS ---

export interface BiomarkerReading {
  key: BiomarkerKey;
  value: number;
  unit: string;
  abnormality: number;
}

export interface FiredRule {
  id: string;
  antecedent: string;
  consequent: Stage;
  firingStrength: number;
}

export interface FISResult {
  stage: Stage;
  confidence: number;
  uncertainty: number;
  needsReview: boolean;
  reviewed: boolean;
  membership: Record<Stage, number>;
  firedRules: FiredRule[];
}

export interface CurvePoint {
  x: number;
  degree: number;
}

export interface BiomarkerDef {
  key: BiomarkerKey;
  label: string;
  shortLabel: string;
  unit: string;
  normalRange: [number, number];
  lowerIsWorse: boolean;
  description: string;
  /** Pre-sampled (x, degree) points per fuzzy set — plot directly, never recompute. */
  curve: Partial<Record<FuzzyLabel, CurvePoint[]>>;
}

export interface RuleDescription {
  id: string;
  antecedent: string;
  consequent: Stage;
}

export interface FISSimulateRequest {
  hippocampalVolume: number;
  ventricleBrainRatio: number;
  corticalThickness: number;
}

// --- Scans ---

export interface ScanRead {
  id: string;
  patientId: string;
  scanDate: string;
  modality: string;
  status: ScanStatus;
  failureReason: string | null;
  biomarkers: BiomarkerReading[];
  fisResult: FISResult | null;
}

export interface ScanSummary {
  id: string;
  scanDate: string;
  status: ScanStatus;
  stage: Stage | null;
  confidence: number | null;
  uncertainty: number | null;
  needsReview: boolean | null;
}

export interface SyntheticScanRequest {
  severity: number;
  seed?: number;
  scanDate?: string;
}

export interface ReviewRequest {
  reviewed: boolean;
}

// --- Patients ---

export interface PatientCreateRequest {
  age: number;
  sex: Sex;
  source?: DataSource;
}

export interface PatientListItem {
  id: string;
  displayId: string;
  age: number;
  sex: Sex;
  source: DataSource;
  latestScan: ScanSummary | null;
}

export interface PatientDetail {
  id: string;
  displayId: string;
  age: number;
  sex: Sex;
  source: DataSource;
  scans: ScanRead[];
}

// --- Cohort ---

export interface CohortStats {
  totalPatients: number;
  totalScans: number;
  needsReview: number;
  avgConfidence: number;
  byStage: Record<Stage, number>;
}

// --- Errors ---

/** Domain errors raised by app/core/exceptions.py (404/409/422/etc). */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
  };
}

/** FastAPI/Pydantic's own request-validation error shape (422 on bad input shape). */
export interface ValidationErrorBody {
  detail: string | { type: string; loc: (string | number)[]; msg: string; input?: unknown }[];
}
