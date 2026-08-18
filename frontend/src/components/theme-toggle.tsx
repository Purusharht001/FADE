import { Moon, Sun } from "lucide-react";
import { useThemeStore } from "@/store/theme";
import { Button } from "@/components/ui/button";

export function ThemeToggle() {
  const { theme, toggle } = useThemeStore();
  return (
    <Button
      variant="outline"
      size="icon"
      onClick={toggle}
      aria-label="Toggle color theme"
      className="relative overflow-hidden"
    >
      <Sun className="size-4 scale-100 rotate-0 transition-all dark:scale-0 dark:-rotate-90" />
      <Moon className="absolute size-4 scale-0 rotate-90 transition-all dark:scale-100 dark:rotate-0" />
      <span className="sr-only">{theme === "dark" ? "Switch to light" : "Switch to dark"}</span>
    </Button>
  );
}
