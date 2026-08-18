import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

const TONE_STROKE: Record<"good" | "warning" | "critical", string> = {
  good: "var(--color-status-good)",
  warning: "var(--color-status-warning)",
  critical: "var(--color-status-critical)",
};

export function ConfidenceRing({
  value,
  tone,
  label,
}: {
  value: number;
  tone: "good" | "warning" | "critical";
  label: string;
}) {
  const size = 128;
  const stroke = 10;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - value / 100);

  return (
    <div className="relative flex items-center justify-center">
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-muted)"
          strokeWidth={stroke}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={TONE_STROKE[tone]}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span
          className={cn(
            "text-2xl font-semibold tabular-nums",
            tone === "good" && "text-status-good",
            tone === "warning" && "text-status-warning",
            tone === "critical" && "text-status-critical"
          )}
        >
          {value}%
        </span>
        <span className="text-[10px] text-muted-foreground">{label}</span>
      </div>
    </div>
  );
}
