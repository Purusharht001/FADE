import { describe, expect, it } from "vitest";
import {
  abnormalityLabel,
  confidenceTone,
  STAGE_BADGE_VARIANT,
  STAGE_LABEL,
} from "@/lib/stage-style";
import type { Stage } from "@/types/api";

const STAGES: Stage[] = ["CN", "MCI", "AD"];

describe("stage display metadata", () => {
  it("has a human label and badge variant for every stage", () => {
    for (const stage of STAGES) {
      expect(STAGE_LABEL[stage]).toBeTruthy();
      expect(STAGE_BADGE_VARIANT[stage]).toBeTruthy();
    }
  });

  it("maps severity consistently: CN=good, MCI=warning, AD=critical", () => {
    expect(STAGE_BADGE_VARIANT.CN).toBe("good");
    expect(STAGE_BADGE_VARIANT.MCI).toBe("warning");
    expect(STAGE_BADGE_VARIANT.AD).toBe("critical");
  });
});

describe("confidenceTone", () => {
  it("is good at/above 75", () => {
    expect(confidenceTone(75)).toBe("good");
    expect(confidenceTone(100)).toBe("good");
  });

  it("is warning between 50 and 75", () => {
    expect(confidenceTone(50)).toBe("warning");
    expect(confidenceTone(74.9)).toBe("warning");
  });

  it("is critical below 50", () => {
    expect(confidenceTone(49.9)).toBe("critical");
    expect(confidenceTone(0)).toBe("critical");
  });
});

describe("abnormalityLabel", () => {
  it("buckets the backend-computed abnormality score into normal/borderline/abnormal", () => {
    expect(abnormalityLabel(0)).toBe("normal");
    expect(abnormalityLabel(0.29)).toBe("normal");
    expect(abnormalityLabel(0.3)).toBe("borderline");
    expect(abnormalityLabel(0.64)).toBe("borderline");
    expect(abnormalityLabel(0.65)).toBe("abnormal");
    expect(abnormalityLabel(1)).toBe("abnormal");
  });
});
