import { AlertCircle, Loader2 } from "lucide-react";
import type { ScanRead } from "@/types/api";

const STATUS_LABEL: Record<ScanRead["status"], string> = {
  pending: "Queued",
  preprocessing: "Preprocessing MRI (skull-strip, bias correction, normalization)…",
  extracting_biomarkers: "Segmenting tissue and extracting biomarkers…",
  running_inference: "Running fuzzy inference…",
  completed: "Completed",
  failed: "Failed",
};

export function ScanStatusBanner({ scan }: { scan: ScanRead }) {
  if (scan.status === "completed") return null;

  if (scan.status === "failed") {
    return (
      <div className="flex items-start gap-3 rounded-lg border border-status-critical/30 bg-status-critical/10 p-4">
        <AlertCircle className="mt-0.5 size-4 shrink-0 text-status-critical" />
        <div>
          <p className="text-sm font-medium text-status-critical">Scan processing failed</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {scan.failureReason ??
              "The pipeline couldn't produce a reliable result for this scan. Try running a new one."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-secondary/40 p-4">
      <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
      <p className="text-sm text-muted-foreground">{STATUS_LABEL[scan.status]}</p>
    </div>
  );
}
