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
