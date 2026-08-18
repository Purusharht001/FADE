import { describe, expect, it } from "vitest";
import { sortPatientsByUncertainty } from "@/lib/sort";
import type { PatientListItem, ScanSummary } from "@/types/api";

function makeScan(overrides: Partial<ScanSummary> = {}): ScanSummary {
  return {
    id: "scan-1",
    scanDate: "2026-01-01",
    status: "completed",
    stage: "MCI",
    confidence: 60,
    uncertainty: 50,
    needsReview: false,
    ...overrides,
  };
}

function makePatient(overrides: Partial<PatientListItem> = {}): PatientListItem {
  return {
    id: crypto.randomUUID(),
    displayId: "PT-0001",
    age: 70,
    sex: "F",
    source: "Clinic",
    latestScan: makeScan(),
    ...overrides,
  };
}

describe("sortPatientsByUncertainty", () => {
  it("orders patients by uncertainty, highest first", () => {
    const low = makePatient({ displayId: "PT-LOW", latestScan: makeScan({ uncertainty: 10 }) });
    const high = makePatient({ displayId: "PT-HIGH", latestScan: makeScan({ uncertainty: 95 }) });
    const mid = makePatient({ displayId: "PT-MID", latestScan: makeScan({ uncertainty: 50 }) });

    const sorted = sortPatientsByUncertainty([low, high, mid]);

    expect(sorted.map((p) => p.displayId)).toEqual(["PT-HIGH", "PT-MID", "PT-LOW"]);
  });

  it("does not mutate the input array", () => {
    const list = [
      makePatient({ displayId: "A", latestScan: makeScan({ uncertainty: 10 }) }),
      makePatient({ displayId: "B", latestScan: makeScan({ uncertainty: 90 }) }),
    ];
    const original = [...list];
    sortPatientsByUncertainty(list);
    expect(list).toEqual(original);
  });

  it("sorts patients with no scan yet to the end", () => {
    const withScan = makePatient({ displayId: "HAS-SCAN", latestScan: makeScan({ uncertainty: 5 }) });
    const noScan = makePatient({ displayId: "NO-SCAN", latestScan: null });

    const sorted = sortPatientsByUncertainty([noScan, withScan]);

    expect(sorted.map((p) => p.displayId)).toEqual(["HAS-SCAN", "NO-SCAN"]);
  });

  it("sorts a scan still mid-pipeline (uncertainty null) to the end too", () => {
    const processing = makePatient({
      displayId: "PROCESSING",
      latestScan: makeScan({ status: "preprocessing", stage: null, confidence: null, uncertainty: null }),
    });
    const completed = makePatient({
      displayId: "COMPLETED",
      latestScan: makeScan({ uncertainty: 1 }),
    });

    const sorted = sortPatientsByUncertainty([processing, completed]);

    expect(sorted.map((p) => p.displayId)).toEqual(["COMPLETED", "PROCESSING"]);
  });

  it("keeps multiple no-scan patients in a stable relative order", () => {
    const a = makePatient({ displayId: "A", latestScan: null });
    const b = makePatient({ displayId: "B", latestScan: null });
    const sorted = sortPatientsByUncertainty([a, b]);
    expect(sorted.map((p) => p.displayId)).toEqual(["A", "B"]);
  });

  it("handles an empty list", () => {
    expect(sortPatientsByUncertainty([])).toEqual([]);
  });

  it("handles ties without throwing and preserves both elements", () => {
    const a = makePatient({ displayId: "A", latestScan: makeScan({ uncertainty: 50 }) });
    const b = makePatient({ displayId: "B", latestScan: makeScan({ uncertainty: 50 }) });
    const sorted = sortPatientsByUncertainty([a, b]);
    expect(sorted).toHaveLength(2);
    expect(sorted.map((p) => p.displayId).sort()).toEqual(["A", "B"]);
  });
});
