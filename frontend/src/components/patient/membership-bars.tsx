import { motion } from "framer-motion";
import type { Stage } from "@/types/api";
import { STAGE_DOT_CLASS } from "@/lib/stage-style";
import { cn } from "@/lib/utils";

const BAR_CLASS: Record<Stage, string> = {
  CN: "bg-status-good",
  MCI: "bg-status-warning",
  AD: "bg-status-critical",
};

const STAGE_ORDER: Stage[] = ["CN", "MCI", "AD"];

export function MembershipBars({ membership }: { membership: Record<Stage, number> }) {
  return (
    <div className="w-full space-y-2.5">
      {STAGE_ORDER.map((stage) => {
        const degree = membership[stage] ?? 0;
        return (
          <div key={stage} className="flex items-center gap-3">
            <span className="flex w-10 items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <span className={cn("size-1.5 rounded-full", STAGE_DOT_CLASS[stage])} />
              {stage}
            </span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
              <motion.div
                className={cn("h-full rounded-full", BAR_CLASS[stage])}
                initial={{ width: 0 }}
                animate={{ width: `${degree * 100}%` }}
                transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
              />
            </div>
            <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
              {Math.round(degree * 100)}%
            </span>
          </div>
        );
      })}
    </div>
  );
}
