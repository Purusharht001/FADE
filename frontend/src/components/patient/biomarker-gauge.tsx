import { motion } from "framer-motion";
import type { BiomarkerDef, BiomarkerReading } from "@/types/api";
import { abnormalityLabel } from "@/lib/stage-style";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const LABEL_VARIANT = {
  normal: "good",
  borderline: "warning",
  abnormal: "critical",
} as const;

export function BiomarkerGauge({ def, reading }: { def: BiomarkerDef; reading: BiomarkerReading }) {
  const label = abnormalityLabel(reading.abnormality);
  // The gauge position comes directly from the backend-computed abnormality
  // score (0 = fully normal, 1 = fully abnormal) — inverted to a 0-100
  // "goodness" axis so every biomarker reads left(bad)-to-right(good)
  // regardless of which raw direction (higher/lower) is clinically worse.
  // No client-side re-derivation from the raw value/breakpoints happens here.
  const markerPct = (1 - reading.abnormality) * 100;

  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{def.label}</p>
          <p className="mt-0.5 text-lg font-semibold tabular-nums">
            {reading.value.toFixed(2)}
            <span className="ml-1 text-xs font-normal text-muted-foreground">{def.unit}</span>
          </p>
        </div>
        <Badge variant={LABEL_VARIANT[label]} className="capitalize">
          {label}
        </Badge>
      </div>

      <div className="relative mt-4 h-2 rounded-full bg-muted">
        <motion.div
          className={cn(
            "absolute top-1/2 size-3.5 -translate-y-1/2 -translate-x-1/2 rounded-full border-2 border-card shadow",
            label === "normal" && "bg-status-good",
            label === "borderline" && "bg-status-warning",
            label === "abnormal" && "bg-status-critical"
          )}
          initial={{ left: "0%" }}
          animate={{ left: `${markerPct}%` }}
          transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
        />
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-muted-foreground">
        <span>Abnormal</span>
        <span>
          Normal range {def.normalRange[0]}–{def.normalRange[1]} {def.unit}
        </span>
        <span>Healthy</span>
      </div>
      <p className="mt-2.5 text-xs leading-snug text-muted-foreground">{def.description}</p>
    </div>
  );
}
