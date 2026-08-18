import { motion } from "framer-motion";
import type { FiredRule } from "@/types/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { STAGE_BADGE_VARIANT } from "@/lib/stage-style";

export function RuleList({ rules }: { rules: FiredRule[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Rule explainability</CardTitle>
        <CardDescription>Fired rules behind this output, ranked by firing strength</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {rules.map((rule, i) => (
          <motion.div
            key={rule.id}
            initial={{ opacity: 0, x: -6 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.3, delay: i * 0.06 }}
            className="rounded-lg border border-border p-3"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-[10px] text-muted-foreground">{rule.id}</p>
                <p className="text-sm leading-snug">
                  {rule.antecedent}{" "}
                  <span className="text-muted-foreground">THEN stage is</span>{" "}
                  <Badge variant={STAGE_BADGE_VARIANT[rule.consequent]} className="align-middle">
                    {rule.consequent}
                  </Badge>
                </p>
              </div>
              <span className="shrink-0 text-sm font-semibold tabular-nums text-muted-foreground">
                {Math.round(rule.firingStrength * 100)}%
              </span>
            </div>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <motion.div
                className="h-full rounded-full bg-primary"
                initial={{ width: 0 }}
                animate={{ width: `${rule.firingStrength * 100}%` }}
                transition={{ duration: 0.6, delay: i * 0.06 + 0.1, ease: [0.16, 1, 0.3, 1] }}
              />
            </div>
          </motion.div>
        ))}
      </CardContent>
    </Card>
  );
}
