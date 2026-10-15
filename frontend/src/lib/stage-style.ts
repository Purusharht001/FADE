import type { Stage } from "@/types/api";

export const STAGE_LABEL: Record<Stage, string> = {
  CN: "Cognitively Normal",
  MCI: "Mild Cognitive Impairment",
  AD: "Alzheimer's Disease",
};

export const STAGE_BADGE_VARIANT: Record<Stage, "good" | "warning" | "critical"> = {
  CN: "good",
  MCI: "warning",
  AD: "critical",
};

export const STAGE_DOT_CLASS: Record<Stage, string> = {
  CN: "bg-status-good",
  MCI: "bg-status-warning",
  AD: "bg-status-critical",
};

export const STAGE_TEXT_CLASS: Record<Stage, string> = {
  CN: "text-status-good",
  MCI: "text-status-warning",
  AD: "text-status-critical",
};

/** Plain-language text for the engine's indeterminate reason codes. */
const REASON_TEXT: Record<string, string> = {
  calibration_incomplete: "The active calibration file still has missing numbers.",
  calibration_not_validated_for_real_data:
    "The active calibration was fitted to the synthetic phantom, not to real scans.",
  calibration_domain_mismatch:
    "This is a phantom scan, but the active calibration was fitted to real data.",
  unit_mismatch: "A biomarker is in a different unit from the calibration",
  missing_biomarker: "A biomarker could not be measured",
  qc_flag: "A measurement failed a plausibility check",
  no_rule_fired: "The values fall outside every fuzzy set, so no rule fired.",
};

/**
 * Turns a backend `indeterminateReason` ("code:detail; code2") into readable
 * lines. Presentation only: the codes and their meaning come from the API.
 */
export function describeIndeterminate(reason: string | null | undefined): string[] {
  if (!reason) return ["The engine declined to stage this scan."];
  return reason.split(";").map((part) => {
    const [code, detail] = part.trim().split(":");
    const text = REASON_TEXT[code] ?? code;
    return detail ? `${text.replace(/\.$/, "")}: ${detail.replace(/_/g, " ").replace(/,/g, ", ")}.` : text;
  });
}

export function confidenceTone(confidence: number): "good" | "warning" | "critical" {
  if (confidence >= 75) return "good";
  if (confidence >= 50) return "warning";
  return "critical";
}

/**
 * Buckets an already backend-computed abnormality score (0-1, see
 * BiomarkerReading.abnormality) into a UI label. This is a presentation
 * concern — choosing a badge color for a number the backend already
 * derived — not a re-derivation of the clinical judgment itself.
 */
export function abnormalityLabel(abnormality: number): "normal" | "borderline" | "abnormal" {
  if (abnormality < 0.3) return "normal";
  if (abnormality < 0.65) return "borderline";
  return "abnormal";
}
