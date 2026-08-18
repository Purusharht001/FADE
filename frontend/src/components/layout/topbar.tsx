import { useLocation, useNavigate } from "react-router-dom";
import { LogOut, Search } from "lucide-react";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAuthStore } from "@/store/auth";

const TITLES: Record<string, { title: string; subtitle: string }> = {
  "/": {
    title: "Triage Dashboard",
    subtitle: "Cases ranked by diagnostic uncertainty, not chart order",
  },
};

export function Topbar() {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  const meta = TITLES[location.pathname] ?? {
    title: "Case Review",
    subtitle: "Volumetric biomarkers and fuzzy staging output",
  };

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-4 border-b border-border bg-background/85 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/70 sm:px-6">
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-base font-semibold tracking-tight">{meta.title}</h1>
        <p className="truncate text-xs text-muted-foreground">{meta.subtitle}</p>
      </div>

      <div className="hidden items-center gap-2 rounded-md border border-border bg-secondary/50 px-3 py-1.5 text-xs text-muted-foreground lg:flex">
        <Search className="size-3.5" />
        <span>Search patient ID…</span>
        <kbd className="ml-4 rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[10px]">
          /
        </kbd>
      </div>

      <Badge variant="outline" className="hidden sm:inline-flex">
        Demo data
      </Badge>

      {user && (
        <span className="hidden max-w-[10rem] truncate text-xs text-muted-foreground md:inline">
          {user.fullName}
        </span>
      )}

      <ThemeToggle />

      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="outline" size="icon" onClick={handleLogout} aria-label="Sign out">
            <LogOut className="size-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Sign out</TooltipContent>
      </Tooltip>
    </header>
  );
}
