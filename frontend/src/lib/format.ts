/**
 * Formats a biomarker measurement for display. Ratios such as VBR sit
 * around 0.03-0.07, where two decimals would hide the difference between a
 * normal and an enlarged value, so values below 1 get three.
 */
export function formatMeasure(value: number): string {
  return Math.abs(value) < 1 ? value.toFixed(3) : value.toFixed(2);
}
