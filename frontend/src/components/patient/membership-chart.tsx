import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { BiomarkerDef, FuzzyLabel } from "@/types/api";

const LABEL_COLOR: Record<FuzzyLabel, string> = {
  low: "var(--color-status-critical)",
  borderline: "var(--color-status-warning)",
  normal: "var(--color-status-good)",
  high: "var(--color-status-critical)",
};

const LABEL_TEXT: Record<FuzzyLabel, string> = {
  low: "Low",
  borderline: "Borderline",
  normal: "Normal",
  high: "High",
};

/**
 * Plots this biomarker's fuzzy membership sets exactly as the backend
 * computed and sampled them (`BiomarkerDef.curve`) — every point here is a
 * number the API returned, not something derived client-side.
 */
export function BiomarkerMembershipChart({ def, value }: { def: BiomarkerDef; value: number }) {
  const labels = Object.keys(def.curve) as FuzzyLabel[];
  // Recharts wants one row per x with a column per series.
  const xs = def.curve[labels[0]]?.map((p) => p.x) ?? [];
  const data = xs.map((x, i) => {
    const row: Record<string, number> = { x };
    for (const label of labels) row[label] = def.curve[label]?.[i]?.degree ?? 0;
    return row;
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{def.shortLabel} membership</CardTitle>
        <CardDescription>
          Fuzzy sets over {def.label.toLowerCase()} — dashed line marks this reading (
          {value.toFixed(2)} {def.unit})
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="h-48 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 4, right: 12, left: 4, bottom: 0 }}>
              <CartesianGrid stroke="var(--color-chart-grid)" vertical={false} />
              <XAxis
                dataKey="x"
                type="number"
                domain={["dataMin", "dataMax"]}
                tick={{ fill: "var(--color-muted-foreground)", fontSize: 11 }}
                axisLine={{ stroke: "var(--color-chart-baseline)" }}
                tickLine={false}
              />
              <YAxis
                domain={[0, 1]}
                ticks={[0, 0.25, 0.5, 0.75, 1]}
                tickFormatter={(v: number) => v.toFixed(2)}
                tick={{ fill: "var(--color-muted-foreground)", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                width={38}
              />
              <RTooltip
                contentStyle={{
                  background: "var(--color-popover)",
                  border: "1px solid var(--color-border)",
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelFormatter={(v) => `${def.shortLabel}: ${Number(v).toFixed(2)} ${def.unit}`}
                formatter={(val, name) => [Number(val).toFixed(2), LABEL_TEXT[name as FuzzyLabel]]}
              />
              <ReferenceLine
                x={value}
                stroke="var(--color-foreground)"
                strokeDasharray="4 4"
                strokeWidth={1.5}
              />
              {labels.map((label) => (
                <Line
                  key={label}
                  type="monotone"
                  dataKey={label}
                  stroke={LABEL_COLOR[label]}
                  strokeWidth={2}
                  dot={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="mt-2 flex items-center justify-center gap-5 text-xs text-muted-foreground">
          {labels.map((label) => (
            <span key={label} className="flex items-center gap-1.5">
              <span className="size-2 rounded-full" style={{ backgroundColor: LABEL_COLOR[label] }} />
              {LABEL_TEXT[label]}
            </span>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
