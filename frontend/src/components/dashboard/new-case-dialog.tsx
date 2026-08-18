import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, UploadCloud } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { createPatient, createSyntheticScan, uploadScan } from "@/api/patients";
import { ApiError } from "@/api/client";
import { toast } from "@/store/toast";
import type { DataSource, Sex } from "@/types/api";

interface NewCaseDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function NewCaseDialog({ open, onOpenChange }: NewCaseDialogProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [age, setAge] = useState("70");
  const [sex, setSex] = useState<Sex>("F");
  const [source, setSource] = useState<DataSource>("Clinic");
  const [method, setMethod] = useState<"synthetic" | "upload">("synthetic");
  const [severity, setSeverity] = useState(0.5);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setAge("70");
    setSex("F");
    setSource("Clinic");
    setMethod("synthetic");
    setSeverity(0.5);
    setFile(null);
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (method === "upload" && !file) {
      setError("Choose an MRI file (.nii or .nii.gz) to upload.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const patient = await createPatient({ age: Number(age), sex, source });

      try {
        if (method === "synthetic") {
          await createSyntheticScan(patient.id, { severity });
        } else {
          await uploadScan(patient.id, file!);
        }
      } catch (scanErr) {
        // The patient record was created successfully even though the scan
        // failed — surface that clearly rather than implying nothing happened,
        // and send the clinician to the record so they can retry the scan.
        queryClient.invalidateQueries({ queryKey: ["patients"] });
        queryClient.invalidateQueries({ queryKey: ["cohort"] });
        const message =
          scanErr instanceof ApiError
            ? scanErr.message
            : "The scan could not be processed.";
        toast({
          variant: "destructive",
          title: "Patient created, but scan processing failed",
          description: message,
        });
        onOpenChange(false);
        reset();
        navigate(`/patient/${patient.id}`);
        return;
      }

      queryClient.invalidateQueries({ queryKey: ["patients"] });
      queryClient.invalidateQueries({ queryKey: ["cohort"] });
      toast({ variant: "success", title: "Case created", description: patient.displayId });
      onOpenChange(false);
      reset();
      navigate(`/patient/${patient.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!submitting) {
          onOpenChange(next);
          if (!next) reset();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New case</DialogTitle>
          <DialogDescription>
            Creates a patient record and runs a scan through the real preprocessing → volumetry →
            fuzzy inference pipeline.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="grid grid-cols-3 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="age">Age</Label>
              <Input
                id="age"
                type="number"
                min={0}
                max={120}
                value={age}
                onChange={(e) => setAge(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sex">Sex</Label>
              <select
                id="sex"
                value={sex}
                onChange={(e) => setSex(e.target.value as Sex)}
                className="h-9 rounded-md border border-input bg-background px-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="F">F</option>
                <option value="M">M</option>
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="source">Source</Label>
              <select
                id="source"
                value={source}
                onChange={(e) => setSource(e.target.value as DataSource)}
                className="h-9 rounded-md border border-input bg-background px-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="Clinic">Clinic</option>
                <option value="OASIS">OASIS</option>
                <option value="ADNI">ADNI</option>
              </select>
            </div>
          </div>

          <Tabs value={method} onValueChange={(v) => setMethod(v as "synthetic" | "upload")}>
            <TabsList className="w-full">
              <TabsTrigger value="synthetic" className="flex-1">
                Synthetic demo scan
              </TabsTrigger>
              <TabsTrigger value="upload" className="flex-1">
                Upload MRI
              </TabsTrigger>
            </TabsList>

            <TabsContent value="synthetic" className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <Label htmlFor="severity">Severity</Label>
                <span className="tabular-nums">{severity.toFixed(2)}</span>
              </div>
              <input
                id="severity"
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={severity}
                onChange={(e) => setSeverity(Number(e.target.value))}
                className="w-full accent-primary"
              />
              <p className="text-xs text-muted-foreground">
                Generates a synthetic MRI phantom and runs it through the real pipeline — there's no
                real imaging dataset wired up yet (see the project README).
              </p>
            </TabsContent>

            <TabsContent value="upload" className="space-y-2">
              <label
                htmlFor="file"
                className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed border-border bg-secondary/30 p-6 text-center transition-colors hover:bg-secondary/50"
              >
                <UploadCloud className="size-5 text-muted-foreground" />
                <span className="text-xs text-muted-foreground">
                  {file ? file.name : "Click to choose a .nii or .nii.gz file"}
                </span>
              </label>
              <input
                id="file"
                type="file"
                accept=".nii,.gz,.nii.gz"
                className="sr-only"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </TabsContent>
          </Tabs>

          {error && (
            <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="submit" disabled={submitting} className="gap-2">
              {submitting && <Loader2 className="size-4 animate-spin" />}
              Create & run
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
