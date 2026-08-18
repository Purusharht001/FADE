import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { AlertTriangle, ChevronRight, Clock, XCircle } from "lucide-react";
import type { PatientListItem } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { STAGE_BADGE_VARIANT, STAGE_DOT_CLASS } from "@/lib/stage-style";
import { cn } from "@/lib/utils";

const progressToneClass: Record<"good" | "warning" | "critical", string> = {
  good: "bg-status-good",
  warning: "bg-status-warning",
  critical: "bg-status-critical",
};

function toneOf(confidence: number): "good" | "warning" | "critical" {
  if (confidence >= 75) return "good";
  if (confidence >= 50) return "warning";
  return "critical";
}

export function PatientRow({ patient, index }: { patient: PatientListItem; index: number }) {
  const scan = patient.latestScan;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, delay: Math.min(index * 0.025, 0.4) }}
    >
      <Link
        to={`/patient/${patient.id}`}
        className="group grid grid-cols-[auto_1fr_auto] items-center gap-4 rounded-lg border border-transparent px-3 py-3 transition-colors hover:border-border hover:bg-secondary/50 sm:grid-cols-[1.4fr_0.8fr_0.9fr_1.4fr_auto_auto]"
      >
        <div className="flex items-center gap-3">
          <span
            className={cn(
              "size-2 shrink-0 rounded-full",
              scan?.stage ? STAGE_DOT_CLASS[scan.stage] : "bg-muted-foreground/40"
            )}
          />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{patient.displayId}</p>
            <p className="text-xs text-muted-foreground">
              {patient.age}y · {patient.sex} · {patient.source}
            </p>
          </div>
        </div>

        <div className="hidden sm:block">
          {scan?.stage ? (
            <Badge variant={STAGE_BADGE_VARIANT[scan.stage]}>{scan.stage}</Badge>
          ) : (
            <Badge variant="outline">No scan</Badge>
          )}
        </div>

        <div className="hidden text-xs text-muted-foreground sm:block">{scan?.scanDate ?? "—"}</div>

        <div className="hidden items-center gap-2 sm:flex">
          {scan?.confidence != null ? (
            <>
              <Progress
                value={scan.confidence}
                className="h-1.5 w-24"
                indicatorClassName={progressToneClass[toneOf(scan.confidence)]}
              />
              <span className="w-9 shrink-0 text-right text-xs font-medium tabular-nums text-muted-foreground">
                {scan.confidence}%
              </span>
            </>
          ) : (
            <span className="text-xs text-muted-foreground">
              {scan?.status === "failed" ? "Processing failed" : scan ? "Processing…" : "—"}
            </span>
          )}
        </div>

        <div className="hidden justify-center sm:flex">
          {scan?.needsReview ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="flex items-center gap-1 text-status-warning">
                  <AlertTriangle className="size-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent>
                High uncertainty ({scan.uncertainty}%) — flagged for clinician review
              </TooltipContent>
            </Tooltip>
          ) : scan?.status === "failed" ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="flex items-center gap-1 text-status-critical">
                  <XCircle className="size-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent>Scan processing failed</TooltipContent>
            </Tooltip>
          ) : scan && scan.status !== "completed" ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="flex items-center gap-1 text-muted-foreground">
                  <Clock className="size-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent>Still processing</TooltipContent>
            </Tooltip>
          ) : (
            <span className="size-3.5" />
          )}
        </div>

        <ChevronRight className="col-start-3 size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 sm:col-start-6" />
      </Link>
    </motion.div>
  );
}
