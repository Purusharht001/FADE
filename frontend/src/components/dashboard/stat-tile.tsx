import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Card } from "@/components/ui/card";

interface StatTileProps {
  label: string;
  value: string | number;
  icon: LucideIcon;
  tone?: "default" | "good" | "warning" | "critical";
  hint?: string;
}

const TONE_CLASS: Record<NonNullable<StatTileProps["tone"]>, string> = {
  default: "bg-primary/10 text-primary",
  good: "bg-status-good/15 text-status-good",
  warning: "bg-status-warning/20 text-status-warning-foreground dark:text-status-warning",
  critical: "bg-status-critical/15 text-status-critical",
};

export function StatTile({ label, value, icon: Icon, tone = "default", hint }: StatTileProps) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium text-muted-foreground">{label}</p>
          <p className="mt-1.5 text-2xl font-semibold tabular-nums tracking-tight">{value}</p>
          {hint && <p className="mt-1 text-[11px] text-muted-foreground">{hint}</p>}
        </div>
        <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-lg", TONE_CLASS[tone])}>
          <Icon className="size-4.5" />
        </div>
      </div>
    </Card>
  );
}
