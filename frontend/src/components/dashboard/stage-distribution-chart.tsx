import { Bar, BarChart, Cell, LabelList, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { CohortStats } from "@/types/api";
import { STAGE_LABEL } from "@/lib/stage-style";

export function StageDistributionChart({ stats }: { stats: CohortStats }) {
  const data = [
    { stage: "CN", label: STAGE_LABEL.CN, count: stats.byStage.CN, color: "var(--color-status-good)" },
    {
      stage: "MCI",
      label: STAGE_LABEL.MCI,
      count: stats.byStage.MCI,
      color: "var(--color-status-warning)",
    },
    {
      stage: "AD",
      label: STAGE_LABEL.AD,
      count: stats.byStage.AD,
      color: "var(--color-status-critical)",
    },
  ];

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle>Cohort by stage</CardTitle>
        <CardDescription>
          Current staging output across all {stats.totalPatients} patients
        </CardDescription>
      </CardHeader>
      <div className="h-40 px-2 pb-4">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ top: 0, right: 28, bottom: 0, left: 0 }}>
            <XAxis type="number" hide domain={[0, "dataMax + 4"]} />
            <YAxis
              type="category"
              dataKey="stage"
              tickLine={false}
              axisLine={false}
              width={40}
              tick={{ fill: "var(--color-muted-foreground)", fontSize: 12 }}
            />
            <Bar dataKey="count" radius={[0, 4, 4, 0]} barSize={20} isAnimationActive={false}>
              {data.map((d) => (
                <Cell key={d.stage} fill={d.color} />
              ))}
              <LabelList
                dataKey="count"
                position="right"
                style={{ fill: "var(--color-foreground)", fontSize: 12, fontWeight: 600 }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
