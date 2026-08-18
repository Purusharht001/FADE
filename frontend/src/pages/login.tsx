import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { Brain, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useLogin, useRegister } from "@/api/auth";
import { useAuthStore } from "@/store/auth";
import { ApiError } from "@/api/client";
import { toast } from "@/store/toast";

export function Login() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const navigate = useNavigate();
  const login = useLogin();
  const registerMutation = useRegister();

  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (isAuthenticated) return <Navigate to="/" replace />;

  const busy = login.isPending || registerMutation.isPending;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (mode === "register") {
        await registerMutation.mutateAsync({ email, password, fullName });
        toast({ variant: "success", title: "Account created", description: "You can now sign in." });
        setMode("login");
        setPassword("");
        return;
      }
      await login.mutateAsync({ email, password });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    }
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-background px-4">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35 }}
        className="w-full max-w-sm"
      >
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex size-11 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <Brain className="size-5.5" />
          </div>
          <div>
            <p className="text-lg font-semibold tracking-tight">FADE</p>
            <p className="text-xs text-muted-foreground">Dementia Staging Dashboard</p>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{mode === "login" ? "Sign in" : "Create an account"}</CardTitle>
            <CardDescription>
              {mode === "login"
                ? "Clinician access to the triage dashboard."
                : "Register a new clinician account."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit} className="flex flex-col gap-3.5">
              {mode === "register" && (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="fullName">Full name</Label>
                  <Input
                    id="fullName"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    required
                    autoComplete="name"
                  />
                </div>
              )}
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={8}
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                />
              </div>

              {error && (
                <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
                  {error}
                </p>
              )}

              <Button type="submit" disabled={busy} className="mt-1 gap-2">
                {busy && <Loader2 className="size-4 animate-spin" />}
                {mode === "login" ? "Sign in" : "Create account"}
              </Button>
            </form>

            <p className="mt-4 text-center text-xs text-muted-foreground">
              {mode === "login" ? "New here?" : "Already have an account?"}{" "}
              <button
                type="button"
                onClick={() => {
                  setMode(mode === "login" ? "register" : "login");
                  setError(null);
                }}
                className="font-medium text-primary hover:underline"
              >
                {mode === "login" ? "Create an account" : "Sign in"}
              </button>
            </p>

            {mode === "login" && (
              <p className="mt-3 rounded-md border border-border bg-secondary/40 px-3 py-2 text-center text-[11px] text-muted-foreground">
                Demo login: <span className="font-mono">clinician@fade.demo</span> /{" "}
                <span className="font-mono">fade-demo-2026</span>
              </p>
            )}
          </CardContent>
        </Card>
      </motion.div>
    </div>
  );
}
