import { NavLink } from "react-router-dom";
import { Activity, Brain, LayoutGrid, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { useCohortStats } from "@/api/cohort";

const NAV_ITEMS = [{ to: "/", label: "Triage Dashboard", icon: LayoutGrid, end: true }];

export function Sidebar() {
  const { data: stats } = useCohortStats();

  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-border bg-card md:flex">
      <div className="flex h-16 items-center gap-2.5 border-b border-border px-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Brain className="size-4.5" />
        </div>
        <div className="leading-tight">
          <p className="text-sm font-semibold tracking-tight">FADE</p>
          <p className="text-[11px] text-muted-foreground">Dementia Staging</p>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5 p-3">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-secondary hover:text-foreground"
              )
            }
          >
            <item.icon className="size-4" />
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="mt-auto flex flex-col gap-3 border-t border-border p-4">
        <div className="rounded-lg border border-border bg-secondary/40 p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <Activity className="size-3.5" />
            Cohort snapshot
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-center">
            <div>
              <p className="text-sm font-semibold tabular-nums text-status-good">
                {stats?.byStage.CN ?? "–"}
              </p>
              <p className="text-[10px] text-muted-foreground">CN</p>
            </div>
            <div>
              <p className="text-sm font-semibold tabular-nums text-status-warning">
                {stats?.byStage.MCI ?? "–"}
              </p>
              <p className="text-[10px] text-muted-foreground">MCI</p>
            </div>
            <div>
              <p className="text-sm font-semibold tabular-nums text-status-critical">
                {stats?.byStage.AD ?? "–"}
              </p>
              <p className="text-[10px] text-muted-foreground">AD</p>
            </div>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-lg border border-border bg-secondary/40 p-3 text-[11px] leading-snug text-muted-foreground">
          <ShieldCheck className="mt-0.5 size-3.5 shrink-0" />
          <span>Synthetic demo data only. Not a certified diagnostic device.</span>
        </div>
      </div>
    </aside>
  );
}
