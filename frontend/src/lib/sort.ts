import type { PatientListItem } from "@/types/api";

/**
 * Orders patients by triage uncertainty, descending — patients whose latest
 * scan is most ambiguous (closest CN/MCI/AD call) come first. The backend
 * already sorts this way by default (`GET /patients?sortByUncertainty=true`);
 * this is a defense-in-depth guarantee at the UI layer so client-side
 * filtering/search can never silently reorder the list away from the triage
 * order a clinician is relying on. It only orders an already-computed
 * number — it never derives one.
 *
 * Patients with no completed scan (`latestScan` null, or a scan still
 * mid-pipeline with no uncertainty yet) sort to the end: there's nothing to
 * triage yet, so they shouldn't crowd out cases that need review.
 */
export function sortPatientsByUncertainty(patients: PatientListItem[]): PatientListItem[] {
  return [...patients].sort((a, b) => {
    const ua = a.latestScan?.uncertainty;
    const ub = b.latestScan?.uncertainty;
    if (ua == null && ub == null) return 0;
    if (ua == null) return 1;
    if (ub == null) return -1;
    return ub - ua;
  });
}
