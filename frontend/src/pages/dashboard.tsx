import { useMemo, useState } from "react";
import { AlertTriangle, Gauge, Plus, ScanLine, Users } from "lucide-react";
import { StatTile } from "@/components/dashboard/stat-tile";
import { StageDistributionChart } from "@/components/dashboard/stage-distribution-chart";
import { PatientRow } from "@/components/dashboard/patient-row";
import { NewCaseDialog } from "@/components/dashboard/new-case-dialog";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { usePatients } from "@/api/patients";
import { useCohortStats } from "@/api/cohort";
import type { Stage } from "@/types/api";
import { cn } from "@/lib/utils";
import { sortPatientsByUncertainty } from "@/lib/sort";

type FilterKey = "all" | "review" | Stage;

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "All cases" },
  { key: "review", label: "Needs review" },
  { key: "CN", label: "CN" },
  { key: "MCI", label: "MCI" },
  { key: "AD", label: "AD" },
];

export function Dashboard() {
  const [filter, setFilter] = useState<FilterKey>("review");
  const [query, setQuery] = useState("");
  const [newCaseOpen, setNewCaseOpen] = useState(false);

  const patientsQuery = usePatients({
    stage: filter === "all" || filter === "review" ? undefined : filter,
    needsReview: filter === "review" ? true : undefined,
    sortByUncertainty: true,
  });
  const cohortQuery = useCohortStats();

  const filtered = useMemo(() => {
    const list = sortPatientsByUncertainty(patientsQuery.data ?? []);
    if (!query.trim()) return list;
    const q = query.trim().toLowerCase();
    return list.filter((p) => p.displayId.toLowerCase().includes(q));
  }, [patientsQuery.data, query]);

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile label="Total scans" value={cohortQuery.data?.totalScans ?? "…"} icon={Users} />
        <StatTile
          label="Flagged for review"
          value={cohortQuery.data?.needsReview ?? "…"}
          icon={AlertTriangle}
          tone="warning"
          hint="High-uncertainty borderline cases"
        />
        <StatTile
          label="Avg. confidence"
          value={cohortQuery.data ? `${cohortQuery.data.avgConfidence}%` : "…"}
          icon={Gauge}
          tone="good"
        />
        <StatTile
          label="AD-stage cases"
          value={cohortQuery.data?.byStage.AD ?? "…"}
          icon={ScanLine}
          tone="critical"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_1.6fr]">
        {cohortQuery.data ? (
          <StageDistributionChart stats={cohortQuery.data} />
        ) : (
          <Card className="h-full min-h-[220px] animate-pulse" />
        )}

        <Card>
          <CardHeader>
            <CardTitle>How triage ranking works</CardTitle>
            <CardDescription>
              Cases aren't sorted by scan date or patient ID — they're sorted by how much the fuzzy
              inference system disagrees with itself.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 text-xs text-muted-foreground sm:grid-cols-3">
            <div className="rounded-lg border border-border bg-secondary/40 p-3">
              <p className="font-medium text-foreground">1. Extract biomarkers</p>
              <p className="mt-1">Hippocampal volume, ventricle-to-brain ratio, cortical thickness.</p>
            </div>
            <div className="rounded-lg border border-border bg-secondary/40 p-3">
              <p className="font-medium text-foreground">2. Fuzzy inference</p>
              <p className="mt-1">
                Membership degrees computed across CN / MCI / AD, rules fire in parallel.
              </p>
            </div>
            <div className="rounded-lg border border-border bg-secondary/40 p-3">
              <p className="font-medium text-foreground">3. Confidence gap</p>
              <p className="mt-1">Close top-two stages → high uncertainty → surfaced first, here.</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-4 space-y-0">
          <div>
            <CardTitle>Case list</CardTitle>
            <CardDescription>
              {patientsQuery.data
                ? `${filtered.length} of ${patientsQuery.data.length} cases shown`
                : "Loading…"}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search patient ID…"
              className="h-8 w-36 rounded-md border border-border bg-background px-3 text-xs outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring sm:w-48"
            />
            <Button size="sm" className="h-8 gap-1.5" onClick={() => setNewCaseOpen(true)}>
              <Plus className="size-3.5" />
              New case
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {FILTERS.map((f) => (
              <Button
                key={f.key}
                size="sm"
                variant={filter === f.key ? "default" : "outline"}
                onClick={() => setFilter(f.key)}
                className="h-7 px-2.5 text-xs"
              >
                {f.label}
                {f.key === "review" && cohortQuery.data && (
                  <span
                    className={cn(
                      "ml-1 rounded-full px-1.5 text-[10px]",
                      filter === f.key
                        ? "bg-primary-foreground/20"
                        : "bg-status-warning/20 text-status-warning-foreground dark:text-status-warning"
                    )}
                  >
                    {cohortQuery.data.needsReview}
                  </span>
                )}
              </Button>
            ))}
          </div>

          <div className="hidden grid-cols-[1.4fr_0.8fr_0.9fr_1.4fr_auto_auto] gap-4 px-3 pb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground sm:grid">
            <span>Patient</span>
            <span>Stage</span>
            <span>Scan date</span>
            <span>Confidence</span>
            <span className="text-center">Flag</span>
            <span />
          </div>

          <div className="divide-y divide-border/60">
            {patientsQuery.isLoading ? (
              <div className="space-y-2 py-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="h-14 animate-pulse rounded-lg bg-muted/60" />
                ))}
              </div>
            ) : patientsQuery.isError ? (
              <div className="py-10 text-center">
                <p className="text-sm font-medium text-status-critical">Couldn't load cases</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {patientsQuery.error instanceof Error
                    ? patientsQuery.error.message
                    : "The API may be unreachable."}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => patientsQuery.refetch()}
                >
                  Retry
                </Button>
              </div>
            ) : filtered.length === 0 ? (
              <p className="py-10 text-center text-sm text-muted-foreground">
                No cases match this filter.
              </p>
            ) : (
              filtered.map((p, i) => <PatientRow key={p.id} patient={p} index={i} />)
            )}
          </div>
        </CardContent>
      </Card>

      <NewCaseDialog open={newCaseOpen} onOpenChange={setNewCaseOpen} />
    </div>
  );
}
